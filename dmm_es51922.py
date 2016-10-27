# -*- coding: utf-8 -*-
"""
Utility for parsing data from multimeters based on Cyrustek ES51922 chipset.
Version 0.9

Written using as much information from the datasheet as possible (some
functionality is not documented).
The utility should output only sensible measurements and checks if the data
packet is valid (there is no check sum in the data packet).

Requires pySerial library from http://pyserial.sourceforge.net/

NOTE: if RS-232 to USB adapter is used make sure the DTR signal is connected
in the adapter. Otherwise, there will be no received data (this is the case with
UNI-T UT61E).

Tested with UNI-T UT61E multimeter.
All the functionality of UNI-T UT61E seems to work fine.
Not tested: temperature and ADP modes.

Licenced LGPL2+
Copyright (C) 2013 Domas Jokubauskis (domas@jokubauskis.lt)

Some information was used from dmmut61e utility by Steffen Vogel
"""

from __future__ import print_function
import serial
import sys
from decimal import Decimal
import struct
import logging
import datetime

"""
baud rate 19230

single byte:
 0V --| |-----------| |-|
      |0|   D0-D6   |P|1|
-3V   |_|___________|_| |__    
        LSB      MSB

whole packet:
range    digit4  digit3
digit2   digit1  digit0
function status  option1
option2  option3 option4
CR LF
"""

# http://wiki.python.org/moin/BitManipulation
# testBit() returns a nonzero result, 2**offset, if the bit at 'offset' is one.
def test_bit(int_type, offset):
    mask = 1 << offset
    return bool(int_type & mask)
    
def get_bits(int_type, template):
    bits = {}
    for i in range(7):
        bit = test_bit(int_type, i)
        bit_name = template[6-i]
        #print(bit, bit_name, i)
        if bit_name in (0,1) and bit==bit_name:
            continue
        elif bit_name in (0,1):
            raise ValueError
        else:
           bits[bit_name] = bit
    return bits

RANGE_VOLTAGE = {
    0b0110000: (1e0, 4, "V"),  #2.2000V
    0b0110001: (1e0, 3, "V"),  #22.000V
    0b0110010: (1e0, 2, "V"),  #220.00V
    0b0110011: (1e0, 1, "V"),  #2200.0V
    0b0110100: (1e-3, 2,"mV"), #220.00mV
}

# undocumented in datasheet
RANGE_CURRENT_AUTO_UA = {
    0b0110000: (1e-6, 2, "uA"), #
    0b0110001: (1e-6, 1, "uA"), #2
}
# undocumented in datasheet
RANGE_CURRENT_AUTO_MA = {
    0b0110000: (1e-3, 3, "mA"), #
    0b0110001: (1e-3, 2, "mA"), #2
}

RANGE_CURRENT_AUTO = { #2-range auto A *It includes auto μA, mA, 22.000A/220.00A, 220.00A/2200.0A.
    0b0110000: "Lower Range (IVSL)", #Current measurement input for 220μA, 22mA.
    0b0110001: "Higher Range (IVSH)" #Current measurement input for 2200μA, 220mA and 22A modes.
} 
RANGE_CURRENT_22A = { 0b0110000: (1e0, 3, "A") } #22.000 A

RANGE_CURRENT_MANUAL = {
    0b0110000: (1e0, 4, "A"), #2.2000A  
    0b0110001: (1e0, 3, "A"), #22.000A  
    0b0110010: (1e0, 2, "A"), #220.00A  
    0b0110011: (1e0, 1, "A"), #2200.0A  
    0b0110100: (1e0, 0, "A"), #22000A  
}

RANGE_ADP = {
    0b0110000: "ADP4",
    0b0110001: "ADP3",
    0b0110010: "ADP2",
    0b0110011: "ADP1",
    0b0110100: "ADP0",
}

RANGE_RESISTANCE = {
    0b0110000: (1e0, 2, "W"), #220.00Ω 
    0b0110001: (1e3, 4, "kW"), #2.2000KΩ
    0b0110010: (1e3, 3, "kW"), #22.000KΩ
    0b0110011: (1e3, 2, "kW"), #220.00KΩ
    0b0110100: (1e6, 4, "MW"), #2.2000MΩ
    0b0110101: (1e6, 3, "MW"), #22.000MΩ
    0b0110110: (1e6, 2, "MW"), #220.00MΩ
}

RANGE_FREQUENCY = {
    0b0110000: (1e0, 1, "Hz"), #22.00Hz  
    0b0110001: (1e0, 1, "Hz"), #220.0Hz  
    #0b0110010                       
    0b0110011: (1e3, 3, "kHz"), #22.000KHz
    0b0110100: (1e3, 2, "kHz"), #220.00KHz
    0b0110101: (1e6, 4, "MHz"), #2.2000MHz
    0b0110110: (1e6, 3, "MHz"), #22.000MHz
    0b0110111: (1e6, 2, "MHz"), #220.00MHz
}

RANGE_CAPACITANCE = {
    0b0110000: (1e-9, 3, "nF"), #22.000nF
    0b0110001: (1e-9, 2, "nF"), #220.00nF
    0b0110010: (1e-6, 4, "uF"), #2.2000μF
    0b0110011: (1e-6, 3, "uF"), #22.000μF
    0b0110100: (1e-6, 2, "uF"), #220.00μF
    0b0110101: (1e-3, 4, "mF"), #2.2000mF
    0b0110110: (1e-3, 3, "mF"), #22.000mF
    0b0110111: (1e-3, 2, "mF"), #220.00mF
}

# When the meter operates in continuity mode or diode mode, this packet is always
# 0110000 since the full-scale ranges in these modes are fixed.
RANGE_DIODE = {
    0b0110000: (1e0, 4, "V"),  #2.2000V
}
RANGE_CONTINUITY = {
    0b0110000: (1e0, 2, "W"), #220.00Ω 
}

FUNCTION = {
    # (function, subfunction, unit)
    0b0111011: ("voltage", RANGE_VOLTAGE, "V"),
    0b0111101: ("current", RANGE_CURRENT_AUTO_UA, "A"), #Auto μA Current / Auto μA Current / Auto 220.00A/2200.0A
    0b0111111: ("current", RANGE_CURRENT_AUTO_MA, "A"), #Auto mA Current   Auto mA Current   Auto 22.000A/220.00A
    0b0110000: ("current", RANGE_CURRENT_22A, "A"), #22 A current
    0b0111001: ("current", RANGE_CURRENT_MANUAL, "A"), #Manual A Current
    0b0110011: ("resistance", RANGE_RESISTANCE, "W"),
    0b0110101: ("continuity", RANGE_CONTINUITY, "W"),
    0b0110001: ("diode", RANGE_DIODE, "V"),
    0b0110010: ("frequency", RANGE_FREQUENCY, "Hz"),
    0b0110110: ("capacitance", RANGE_CAPACITANCE, "F"),
    0b0110100: ("temperature", None, "deg"),
    0b0111110: ("ADP", RANGE_ADP, ""),
}

DIGITS = {
    0b0110000: 0,
    0b0110001: 1,
    0b0110010: 2,
    0b0110011: 3,
    0b0110100: 4,
    0b0110101: 5,
    0b0110110: 6,
    0b0110111: 7,
    0b0111000: 8,
    0b0111001: 9,
}

STATUS = [
    0, 1, 1,
    "Judge", # 1-°C, 0-°F.
    "Sign", # 1-minus sign, 0-no sign
    "BATT", # 1-battery low
    "OL", # input overflow
]

OPTION1 = [
    0, 1, 1,
    "MAX", # maximum
    "MIN", # minimum
    "REL", # relative/zero mode
    "RMR", # current value
]

OPTION2 = [
    0, 1, 1,
    "UL", # 1 -at 22.00Hz <2.00Hz., at 220.0Hz <20.0Hz,duty cycle <10.0%.
    "PMAX", #  maximum peak value
    "PMIN", # minimum peak value
    0,
]

OPTION3 = [
    0, 1, 1,
    "DC", # DC measurement mode, either voltage or current. 
    "AC", # AC measurement mode, either voltage or current.
    "AUTO", # 1-automatic mode, 0-manual
    "VAHZ",
]

OPTION4 = [
    0, 1, 1, 0,
    "VBAR", # 1-VBAR pin is connected to V-.
    "Hold", # hold mode
    "LPF", #low-pass-filter feature is activated.
]

def parse(packet):
    #packet = [ord(byte) for byte in packet]
    d_range, \
    d_digit4, d_digit3, d_digit2, d_digit1, d_digit0, \
    d_function, d_status, \
    d_option1, d_option2, d_option3, d_option4 = struct.unpack("B"*12, packet)
    
    mode = FUNCTION[d_function][0]
    m_range =  FUNCTION[d_function][1][d_range]
    unit = FUNCTION[d_function][2]
    
    options = {}
    d_options = (d_status, d_option1, d_option2, d_option3, d_option4)
    OPTIONS = (STATUS, OPTION1, OPTION2, OPTION3, OPTION4)
    for d_option, OPTION in zip(d_options, OPTIONS):
        bits = get_bits(d_option, OPTION)
        options.update(bits)
        
    current = None
    if options["AC"] and options["DC"]:
        raise ValueError
    elif options["DC"]:
        current = "AC"
    elif options["AC"]:
        current = "DC"
        
    operation = "normal"
    # sometimes there a glitch where both UL and OL are enabled in normal operation
    # so no error is raised when it occurs
    if options["UL"]:
        operation = "underload"
    elif options["OL"]:
        operation = "overload"
        
    if options["AUTO"]:
        mrange = "auto"
    else:
        mrange = "manual"
        
    if options["BATT"]:
        battery_low = True
    else:
        battery_low = False
    
    # relative measurement mode, received value is actual!
    if options["REL"]:
        relative = True
    else:
        relative = False
    
    # data hold mode, received value is actual!
    if options["Hold"]:
        hold = True
    else:
        hold = False
        
    peak = None
    if options["MAX"]:
        peak = "max"
    elif options["MIN"]:
        peak = "min"
    
    if mode == "current" and options["VBAR"]:
        pass
        """Auto μA Current
        Auto mA Current"""
    elif mode == "current" and not options["VBAR"]:
        pass
        """Auto 220.00A/2200.0A
        Auto 22.000A/220.00A"""
    
    if options["VAHZ"] and not options["Judge"]:
        mode = "frequency"
        unit = "Hz"
        m_range = (1e0, 1, "Hz") #2200.0°C
    elif (options["VAHZ"] or mode == "frequency") and options["Judge"]:
        mode = "duty_cycle"
        unit = "%"
        m_range = (1e0, 1, "%") #2200.0°C
        
    if mode == "temperature" and options["VBAR"]:
        m_range = (1e0, 1, "deg") #2200.0°C
    elif mode == "temperature" and not options["VBAR"]:
        m_range = (1e0, 2, "deg") #220.00°C and °F
        
    digits = [d_digit4, d_digit3, d_digit2, d_digit1, d_digit0]
    digits = [DIGITS[digit] for digit in digits]
    
    display_value = 0
    for i, digit in zip(range(5), digits):
        display_value += digit*(10**(4-i))
        
    # negative value
    if options["Sign"]:
        display_value = display_value * -1
    
    display_value = Decimal(display_value) / 10**m_range[1]
    display_value = display_value.quantize(Decimal(1)/10**m_range[1])
    display_unit = m_range[2]
    value = float(display_value) * m_range[0]
    
    if operation != "normal":
        display_value = ""
        value = ""
    results = {
        "value": value,
        "unit": unit,
        "display_value": display_value,
        "display_unit": display_unit,
        "mode": mode,
        "current": current,
        "peak": peak,
        "relative": relative,
        "hold": hold,
        #"range": mrange,
        "operation": operation,
        "battery_low": battery_low
    }
    
    return results

def output_readable(results):
    operation = results["operation"]
    battery_low = results["battery_low"]
    if operation == "normal":
        display_value = results["display_value"]
        display_unit = results["display_unit"]
        line = "{value} {unit}".format(value=display_value, unit=display_unit)
    else:
        line = "-, the measurement is {operation}ed!".format(operation=operation)
    if battery_low:
        line.append(" Battery low!")
    return line

CSV_FIELDS = ["value", "unit", "mode", "current", "operation", "peak", 
            "battery_low", "relative", "hold"]
def format_field(results, field_name):
    value = results[field_name]
    if field_name == "value":
        if results["operation"]=="normal":
            return str(value)
        else:
            return ""
    if value==None:
        return ""
    elif value==True:
        return "1"
    elif value==False:
        return "0"
    else:
        return str(value)
        
def output_csv(results):
    field_data = [format_field(results, field_name) for field_name in CSV_FIELDS]
    line = ";".join(field_data)
    return line

def main():
    import argparse
    parser = argparse.ArgumentParser(description='Upload time data files.')
    default_port = '/dev/ttyUSB0'
    parser.add_argument('port',
                        help='multimeter port (/dev/tty0, /dev/ttyUSB0, etc.)')
    parser.add_argument('-m', '--mode', choices=['csv', 'readable'],
                        default="csv",
                        help='output mode (default: csv)')
    parser.add_argument('-f', '--file',
                        help='output file')
    parser.add_argument('--verbose', action='store_true',
                        help='the program is verbose about its work')
    #parser.add_argument('--port', help='config file', default="time_data_upload.cfg")
    args = parser.parse_args()
    
    if args.verbose:
        log_level = logging.DEBUG
    else:
        log_level = logging.INFO
    logging.basicConfig(format='%(levelname)s:%(message)s', level=log_level)
    
    logging.info('Using port "{port}" in "{mode}" mode."'.format(port=args.port, 
                                                                 mode=args.mode))
    try:
        ser = serial.Serial(port = args.port,
                baudrate = 19200,
                bytesize=serial.SEVENBITS,
                stopbits = serial.STOPBITS_ONE,
                parity = serial.PARITY_ODD,
                timeout=15) # default timeout for reading in seconds
    # exit if the port is not opened
    except serial.SerialException, e:
        sys.exit(e)
    ser.dtr = True
    ser.rts = False
    output_file = None
    if args.mode == 'csv':
        timestamp = datetime.datetime.now()
        date_format = "%Y-%m-%d_%H:%S"
        timestamp = timestamp.strftime(date_format)
        if args.file:
            file_name = args.file
        else:
            file_name = "measurement_{}.csv".format(timestamp)
        output_file = open(file_name, "w")
        logging.info('Writing to file "{}"'.format(file_name))
        header = "timestamp;{}\n".format(";".join(CSV_FIELDS))
        output_file.write(header)
    while True:
        line = ser.readline()
        line = line.strip()
        timestamp = datetime.datetime.now()
        timestamp = timestamp.isoformat(sep=' ')
        if len(line)==12:
            try:
                results = parse(line)
            except Exception, e:
                logging.warning('Error "{}" packet from multimeter: "{}"'.format(e, line))
            if args.mode == 'csv':
                line = output_csv(results)
                output_file.write("{};{}\n".format(timestamp, line))
            elif args.mode == 'readable':
                pass
            else:
                raise NotImplementedError
            line = output_readable(results)
            print(timestamp.split(" ")[1], line)
        elif line:
            logging.warning('Unknown packet from multimeter: "{}"'.format(line))
        else:
            logging.warning("No response from multimeter")
    ser.close()
    
if __name__ == "__main__":
    main()