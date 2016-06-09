#!/usr/bin/env python3
#
# Copyright (C) 2016 Zubax Robotics <info@zubax.com>
#
# This program is free software: you can redistribute it and/or modify it under the terms of the
# GNU General Public License as published by the Free Software Foundation, either version 3 of the License,
# or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
# without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this program.
# If not, see <http://www.gnu.org/licenses/>.
#
# Author: Pavel Kirienko <pavel.kirienko@zubax.com>
#

import os
import sys
sys.path.insert(1, os.path.join(sys.path[0], 'pyuavcan'))

from drwatson import init, run, make_api_context_with_user_provided_credentials, execute_shell_command,\
    info, error, input, CLIWaitCursor, download, abort, glob_one, download_newest, open_serial_port,\
    enforce, SerialCLI, catch, BackgroundSpinner, fatal, warning, BackgroundDelay, imperative, \
    load_firmware_via_gdb
import logging
import time
import yaml
import binascii
from base64 import b64decode, b64encode


PRODUCT_NAME = 'io.px4.sapog'
DEFAULT_FIRMWARE_GLOB = 'https://files.zubax.com/products/%s/*.compound.bin' % PRODUCT_NAME
CAN_BITRATE = 125000
FLASH_OFFSET = 0x08000000
TOOLCHAIN_PREFIX = 'arm-none-eabi-'
DEBUGGER_PORT_GDB_GLOB = '/dev/serial/by-id/*Black_Magic_Probe*-if00'
DEBUGGER_PORT_CLI_GLOB = '/dev/serial/by-id/*Black_Magic_Probe*-if02'
BOOT_TIMEOUT = 9


logger = logging.getLogger('main')


args = init('''Production testing application for ESC based on PX4 Sapog open source firmware.
If you're a licensed manufacturer, you should have received usage
instructions with the manufacturing doc pack.''',
            lambda p: p.add_argument('iface', help='CAN interface or device path, e.g. "can0", "/dev/ttyACM0", etc.'),
            lambda p: p.add_argument('--firmware', '-f', help='location of the firmware file (if not provided, ' +
                                     'the firmware will be downloaded from Zubax Robotics file server)'),
            require_root=True)

info('''
Usage instructions:

1. Connect a CAN adapter to this computer. Supported adapters are:
1.1. SLCAN-compliant adapters. If you're using an SLCAN adapter,
     use its serial port name as CAN interface name (e.g. "/dev/ttyACM0").
1.2. SocketCAN-compatible adapters. In this case it is recommended to use
     8devices USB2CAN. Correct interface name would be "can0".

2. Connect exactly one DroneCode Probe to this computer.
   For more info refer to https://docs.zubax.com/dronecode_probe.

3. Follow the instructions printed in green. If you have any questions,
   don't hesitate to reach licensing@zubax.com, or use the emergency
   contacts provided to you earlier.
''')


def wait_for_boot():
    deadline = time.monotonic() + BOOT_TIMEOUT

    def handle_serial_port_hanging():
        fatal('DRWATSON HAS DETECTED A PROBLEM WITH CONNECTED HARDWARE AND NEEDS TO TERMINATE.\n'
              'A serial port operation has timed out. This usually indicates a problem with the connected '
              'hardware or its drivers. Please disconnect all USB devices currently connected to this computer, '
              "then connect them back and restart Drwatson. If you're using a virtual machine, please reboot it.",
              use_abort=True)

    with BackgroundDelay(BOOT_TIMEOUT * 5, handle_serial_port_hanging):
        with open_serial_port(DEBUGGER_PORT_CLI_GLOB, timeout=BOOT_TIMEOUT) as p:
            try:
                for line in p:
                    if PRODUCT_NAME.encode() in line:
                        return
                    logger.info('CLI output: %s', line)
                    if time.monotonic() > deadline:
                        break
            except IOError:
                logging.info('Boot error', exc_info=True)
            finally:
                p.flushInput()

    warning("The board did not report to CLI with a correct boot message, but we're going "
            "to continue anyway. Possible reasons for this warning:\n"
            '1. The board could not boot properly (however it was flashed successfully).\n'
            '2. The debug connector is not soldered properly.\n'
            '3. The serial port is open by another application.\n'
            '4. Either USB-UART adapter or VM are malfunctioning. Try to re-connect the '
            'adapter (disconnect from USB and from the board!) or reboot the VM.')


def init_can_iface():
    if '/' not in args.iface:
        logger.debug('Using iface %r as SocketCAN', args.iface)
        execute_shell_command('ifconfig %s down && ip link set %s up type can bitrate %d sample-point 0.875',
                              args.iface, args.iface, CAN_BITRATE)
        return args.iface
    else:
        logger.debug('Using iface %r as SLCAN', args.iface)

        speed_code = {
            1000000: 8,
            500000: 6,
            250000: 5,
            125000: 4,
            100000: 3
        }[CAN_BITRATE]

        execute_shell_command('killall -INT slcand &> /dev/null', ignore_failure=True)
        time.sleep(1)

        tty = os.path.realpath(args.iface).replace('/dev/', '')
        logger.debug('TTY %r', tty)

        execute_shell_command('slcan_attach -f -o -s%d /dev/%s', speed_code, tty)
        execute_shell_command('slcand %s', tty)

        iface_name = 'slcan0'
        time.sleep(1)
        execute_shell_command('ifconfig %s up', iface_name)
        execute_shell_command('ifconfig %s txqueuelen 1000', iface_name)

        return iface_name


def check_interfaces():
    ok = True

    def test_serial_port(glob, name):
        try:
            with open_serial_port(glob):
                info('%s port is OK', name)
                return True
        except Exception:
            error('%s port is not working', name)
            return False

    info('Checking interfaces...')
    ok = test_serial_port(DEBUGGER_PORT_GDB_GLOB, 'GDB') and ok
    ok = test_serial_port(DEBUGGER_PORT_CLI_GLOB, 'CLI') and ok
    try:
        init_can_iface()
        info('CAN interface is OK')
    except Exception:
        logging.debug('CAN check error', exc_info=True)
        error('CAN interface is not working')
        ok = False

    if not ok:
        fatal('Required interfaces are not available. Please check your hardware configuration. '
              'If this application is running on a virtual machine, make sure that hardware '
              'sharing is configured correctly.')

check_interfaces()

licensing_api = make_api_context_with_user_provided_credentials()

with CLIWaitCursor():
    print('Please wait...')
    if args.firmware:
        firmware_data = download(args.firmware)
    else:
        firmware_data = download_newest(DEFAULT_FIRMWARE_GLOB)
    assert 30 < (len(firmware_data) / 1024) <= 240, 'Invalid firmware size'


def process_one_device():
    out = input('1. Connect DroneCode Probe to the debug connector\n'
                '2. Connect CAN to the first CAN1 connector on the device; terminate the other CAN1 connector\n'
                '3. If you want to skip firmware upload, type F\n'
                '4. Press ENTER')

    skip_fw_upload = 'f' in out.lower()
    if not skip_fw_upload:
        info('Loading the firmware')
        with CLIWaitCursor():
            load_firmware_via_gdb(firmware_data,
                                  toolchain_prefix=TOOLCHAIN_PREFIX,
                                  load_offset=FLASH_OFFSET,
                                  gdb_port=glob_one(DEBUGGER_PORT_GDB_GLOB),
                                  gdb_monitor_scan_command='swdp_scan')
        info('Waiting for the board to boot...')
        wait_for_boot()
    else:
        info('Firmware upload skipped')

    info('Testing UAVCAN interface...')
    #test_uavcan()

    info('Connecting via CLI...')
    with open_serial_port(DEBUGGER_PORT_CLI_GLOB) as io:
        cli = SerialCLI(io, 0.1)
        cli.flush_input(0.5)

        try:
            # Using first command to get rid of any garbage lingering in the buffers
            cli.write_line_and_read_output_lines_until_timeout('systime')
        except Exception:
            pass

        zubax_id = cli.write_line_and_read_output_lines_until_timeout('zubax_id')
        zubax_id = yaml.load('\n'.join(zubax_id))
        logger.info('Zubax ID: %r', zubax_id)

        unique_id = b64decode(zubax_id['hw_unique_id'])

        # Getting the signature
        info('Requesting signature for unique ID %s', binascii.hexlify(unique_id).decode())
        gensign_response = licensing_api.generate_signature(unique_id, PRODUCT_NAME)
        if gensign_response.new:
            info('New signature has been generated')
        else:
            info('This particular device has been signed earlier, reusing existing signature')
        base64_signature = b64encode(gensign_response.signature).decode()
        logger.info('Generated signature in Base64: %s', base64_signature)

        # Installing the signature; this may fail if the device has been signed earlier - the failure will be ignored
        out = cli.write_line_and_read_output_lines_until_timeout('zubax_id %s', base64_signature)
        logger.debug('Signature installation response (may fail, which is OK): %r', out)

        # Reading the signature back and verifying it
        zubax_id = cli.write_line_and_read_output_lines_until_timeout('zubax_id')
        zubax_id = yaml.load('\n'.join(zubax_id))
        out = zubax_id['hw_signature']
        enforce(len(out) == 1, 'Could not read the signature back. Returned lines: %r', out)
        logger.info('Installed signature in Base64: %s', out[0])
        enforce(b64decode(out[0]) == gensign_response.signature,
                'Written signature does not match the generated signature')

        info('Signature has been installed and verified')

run(process_one_device)
