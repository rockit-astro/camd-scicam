#
# This file is part of the Robotic Observatory Control Kit (rockit)
#
# rockit is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# rockit is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with rockit.  If not, see <http://www.gnu.org/licenses/>.

"""Helper process for interfacing with the EPIX SDK"""

# pylint: disable=too-many-arguments
# pylint: disable=too-many-instance-attributes
# pylint: disable=too-many-return-statements
# pylint: disable=too-many-branches
# pylint: disable=too-many-statements
# pylint: disable=broad-exception-raised
# pylint: disable=too-many-nested-blocks

from ctypes import byref, c_char, c_char_p, c_double, c_int, c_uint8, c_uint16, c_uint64, c_void_p
from ctypes import create_string_buffer, POINTER, Structure
import json
import pathlib
import platform
import sys
import threading
import time
import traceback
from astropy.time import Time
import astropy.units as u
import numpy as np
import Pyro4
from rockit.common import log
from .constants import CommandStatus, CameraStatus, CoolerMode


class SciCamInterface:
    def __init__(self, config, processing_queue,
                 processing_framebuffer, processing_framebuffer_offsets,
                 processing_stop_signal):
        self._config = config

        self._handle = c_void_p()
        self._xclib = None
        self._lock = threading.Lock()

        self._camera_model = ''
        self._camera_serial = ''
        self._camera_firmware = ''
        self._camera_software = ''
        self._camera_library = ''
        self._grabber_model = ''
        self._readout_width = 0
        self._readout_height = 0

        self._cooler_mode = CoolerMode.Unknown
        self._cooler_setpoint = config.cooler_setpoint
        self._cooler_voltage = 0
        self._sensor_temperature = 0
        self._digpcb_temperature = 0
        self._senspcb_temperature = 0
        self._case_temperature = 0

        self._exposure_time = 1

        # Limit and number of frames acquired during the next sequence
        # Set to 0 to run continuously
        self._sequence_frame_limit = 0

        # Number of frames acquired this sequence
        self._sequence_frame_count = 0

        # Time that the latest frame in the exposure was started
        self._sequence_exposure_start_time = Time.now()

        # Information for building the output filename
        self._output_directory = pathlib.Path(config.output_path)
        self._output_frame_prefix = config.output_prefix

        # Persistent frame counters
        self._counter_filename = config.expcount_path
        try:
            with open(self._counter_filename, 'r', encoding='ascii') as infile:
                data = json.load(infile)
                self._exposure_count = data['exposure_count']
                self._exposure_count_reference = data['exposure_reference']
        except Exception:
            now = Time.now().strftime('%Y-%m-%d')
            self._exposure_count = 0
            self._exposure_count_reference = now

        # Thread that runs the exposure sequence
        # Initialized by start() method
        self._acquisition_thread = None

        # Signal that the exposure sequence should be terminated
        # at end of the current frame
        self._stop_acquisition = False

        # Subprocess for processing acquired frames
        self._processing_queue = processing_queue
        self._processing_stop_signal = processing_stop_signal

        # A large block of shared memory for sending frame data to the processing workers
        self._processing_framebuffer = processing_framebuffer

        # A queue of memory offsets that are available to write frame data into
        # Offsets are popped from the queue as new frames are written into the
        # frame buffer, and pushed back on as processing is complete
        self._processing_framebuffer_offsets = processing_framebuffer_offsets

    @property
    def is_acquiring(self):
        return self._acquisition_thread is not None and self._acquisition_thread.is_alive()

    def update_cooler(self):
        """Polls and updates cooler status"""
        try:
            with self._lock:
                # Query temperature status
                self._sensor_temperature = float(self._serial_command('TEMP:SENS?'))
                self._digpcb_temperature = float(self._serial_command('TEMP:DIGPCB?'))
                self._senspcb_temperature = float(self._serial_command('TEMP:SENSPCB?'))
                self._case_temperature = float(self._serial_command('TEMP:CASE?'))
                self._cooler_voltage = float(self._serial_command('TEC:V?'))

                if self._serial_command('TEC:EN?') == 'ON':
                    if self._serial_command('TEC:LOCK?') == 'ON':
                        self._cooler_mode = CoolerMode.Locked
                    else:
                        self._cooler_mode = CoolerMode.Locking
                else:
                    self._cooler_mode = CoolerMode.Off
        except:
            self._cooler_mode = CoolerMode.Unknown

    def _serial_command(self, command, timeout=5):
        """
        Formats and sends a command message to the camera and reads its response.
        The caller is assumed to already be holding the xclib lock
        """
        self._xclib.pxd_serialFlush(1, 0, 1, 1)

        data = (command + '\r').encode('ascii')
        ret = self._xclib.pxd_serialWrite(1, 0, data, len(data))
        if ret < 0:
            raise Exception(f'failed to send command: {command}')

        buf = create_string_buffer(255)
        response = ''

        wait_start = Time.now()
        while True:
            available = self._xclib.pxd_serialRead(1, 0, buf, 255)
            if available > 0:
                response += buf.value[:available].decode('ascii')
                if response[-1] == '>':
                    break

            if Time.now() - wait_start > timeout * u.s:
                raise Exception('timeout while waiting for command response')

            time.sleep(0.001)

        response = response.split('\r')
        if response[0] != command:
            raise Exception(f'echo response mismatch: `{command}` != `{response[0]}`')

        if response[1] == '>':
            return None

        if response[1].startswith('ERR:'):
            raise Exception(f'command `{command}`: {response[1]}')

        return response[1]

    def __run_exposure_sequence(self, quiet):
        """Worker thread that acquires frames and their times.
           Tagged frames are pushed to the acquisition queue
           for further processing on another thread"""
        framebuffer_slots = 0
        try:
            with self._lock:
                current_exposure_counts = int(self._serial_command('SENS:EXPPER?'))
                exposure_counts = int(self._exposure_time * 15e6)

                if exposure_counts > current_exposure_counts:
                    # Increase the frame period to be longer than the desired exposure
                    period_counts = int(self._serial_command(f'SENS:FRAMEPER:MIN?'))
                    period_counts = max(exposure_counts + 1000, period_counts)
                    self._serial_command(f'SENS:FRAMEPER {period_counts}')

                self._serial_command(f'SENS:EXPPER {exposure_counts}')

                # Reduce the frame period to its minimum length
                period_counts = int(self._serial_command(f'SENS:FRAMEPER:MIN?'))
                self._serial_command(f'SENS:FRAMEPER {period_counts}')

                # Sync the onboard clock to reset any clock drift
                # This is not going to be better than 1 second accuracy between sequences...
                self._serial_command(f'TIME {Time.now().strftime("%Y:%m:%d:%H:%M:%S")}')

            # Prepare the framebuffer offsets
            if not self._processing_framebuffer_offsets.empty():
                log.error(self._config.log_name, 'Frame buffer offsets queue is not empty!')
                return

            with self._lock:
                dma_buffer_count = self._xclib.pxd_imageZdim(1)

            pixel_count = self._readout_width * self._readout_height
            frame_size = 2 * pixel_count
            output_buffer_count = len(self._processing_framebuffer) // frame_size
            if output_buffer_count != dma_buffer_count:
                size = dma_buffer_count * frame_size
                print(f'warning: framebuffer_bytes should be set to {size} for optimal performance')

            offset = 0
            framebuffer_slot_arrays = {}
            while offset + frame_size <= len(self._processing_framebuffer):
                self._processing_framebuffer_offsets.put(offset)
                framebuffer_slot_arrays[offset] = np.frombuffer(self._processing_framebuffer,
                                                                offset=offset, dtype=np.uint16,
                                                                count=pixel_count)
                offset += frame_size
                framebuffer_slots += 1
                if framebuffer_slots == dma_buffer_count:
                    break

            with self._lock:
                last_buffer = self._xclib.pxd_capturedBuffer(1)
                # Queue all available buffers to start sequence
                for i in range(8):
                    self._xclib.pxd_quLive(1, i + 1)

            readout_buffer = bytearray(2 * pixel_count)
            readout_array = np.frombuffer(readout_buffer, dtype=np.uint16)
            readout_cdata = (c_uint16 * pixel_count).from_buffer(readout_buffer)

            with self._lock:
                self._serial_command('SENS:TRIG ON')

            while not self._stop_acquisition and not self._processing_stop_signal.value:
                self._sequence_exposure_start_time = Time.now()
                framebuffer_offset = self._processing_framebuffer_offsets.get()

                # Wait for frame to become available
                while True:
                    with self._lock:
                        buffer = self._xclib.pxd_capturedBuffer(1)

                    if buffer != last_buffer or self._stop_acquisition or self._processing_stop_signal.value:
                        break
                    time.sleep(0.001)

                if self._stop_acquisition or self._processing_stop_signal.value:
                    # Return unused slot back to the queue to simplify cleanup
                    self._processing_framebuffer_offsets.put(framebuffer_offset)
                    break

                last_buffer = buffer

                with self._lock:
                    field = self._xclib.pxd_buffersFieldCount(1, buffer)
                    ret = self._xclib.pxd_readushort(1, buffer, 0, 0, self._readout_width, self._readout_height,
                                                     readout_cdata, pixel_count, b'GREY')
                    self._xclib.pxd_quLive(1, buffer)

                    if ret < 0:
                        print(f'Failed to read frame data: {self._xclib.pxd_mesgErrorCode(ret)}')
                        continue

                read_end_time = Time.now()

                framebuffer_slot_arrays[framebuffer_offset][:] = readout_array[:]

                self._processing_queue.put({
                    'data_offset': framebuffer_offset,
                    'data_width': self._readout_width,
                    'data_height': self._readout_height,
                    'exposure': float(self._exposure_time),
                    'frameperiod': period_counts / 15e6,
                    'field': field,
                    'read_end_time': read_end_time,
                    'cooler_mode': self._cooler_mode,
                    'cooler_setpoint': self._cooler_setpoint,
                    'cooler_voltage': self._cooler_voltage,
                    'sensor_temperature': self._sensor_temperature,
                    'case_temperature': self._case_temperature,
                    'senpcb_temperature': self._senspcb_temperature,
                    'digpcb_temperature': self._digpcb_temperature,
                    'camera_library': self._camera_library,
                    'camera_model': self._camera_model,
                    'camera_serial': self._camera_serial,
                    'firmware_version': self._camera_firmware,
                    'software_version': self._camera_software,
                    'grabber_model': self._grabber_model,
                    'exposure_count': self._exposure_count,
                    'exposure_count_reference': self._exposure_count_reference
                })

                self._exposure_count += 1
                self._sequence_frame_count += 1

                # Continue exposure sequence?
                if 0 < self._sequence_frame_limit <= self._sequence_frame_count:
                    self._stop_acquisition = True
        except Exception as e:
            print(e)
        finally:
            with self._lock:
                self._xclib.pxd_goUnLive(1)
                self._serial_command('SENS:TRIG OFF')

            # Save updated counts to disk
            with open(self._counter_filename, 'w', encoding='ascii') as outfile:
                json.dump({
                    'exposure_count': self._exposure_count,
                    'exposure_reference': self._exposure_count_reference,
                }, outfile)

            # Wait for processing to complete
            for _ in range(framebuffer_slots):
                self._processing_framebuffer_offsets.get()

            if not quiet:
                log.info(self._config.log_name, 'Exposure sequence complete')
            self._stop_acquisition = False

    def initialize(self):
        """Connects to the camera driver"""
        print('initializing frame grabber')
        initialized = False
        with self._lock:
            pixci_args = b'-CQ 8'
            # pylint: disable=import-outside-toplevel
            if platform.system() == 'Windows':
                from ctypes import WinDLL
                self._xclib = WinDLL(r'C:\Program Files\EPIX\XCLIB\lib\xclibw64.dll')
            else:
                from ctypes import CDLL
                self._xclib = CDLL('/usr/local/xclib/lib/xclib_x86_64.so')
            # pylint: enable=import-outside-toplevel

            self._xclib.pxd_PIXCIopen.argtypes = [c_char_p, c_char_p, c_char_p]
            self._xclib.pxd_serialWrite.argtypes = [c_int, c_int, POINTER(c_char), c_int]
            self._xclib.pxd_serialConfigure.argtypes = [
                c_int, c_int, c_double, c_int, c_int, c_int, c_int, c_int, c_int]
            self._xclib.pxd_readushort.argtypes = [
                c_int, c_int, c_int, c_int, c_int, c_int, c_void_p, c_int, c_char_p]
            self._xclib.pxd_mesgErrorCode.restype = c_char_p
            self._xclib.pxd_infoDriverId.restype = c_char_p
            self._xclib.pxd_infoLibraryId.restype = c_char_p

            try:
                ret = self._xclib.pxd_PIXCIopen(pixci_args,
                                                None,
                                                self._config.camera_config_path.encode('ascii'))
                if ret != 0:
                    print(f'Failed to open PIXCI: {self._xclib.pxd_mesgErrorCode(ret)}')
                    return 1

                self._camera_library = self._xclib.pxd_infoLibraryId(1).decode('ascii')
                grabber_model = self._xclib.pxd_infoModel(1)
                if grabber_model == 0x0030:
                    self._grabber_model = 'PIXCI_E8'
                else:
                    self._grabber_model = 'UNKNOWN ({grabber_model:04x})'

                ret = self._xclib.pxd_serialConfigure(1, 0, 115200, 8, 0, 1, 0, 0, 0)
                if ret != 0:
                    print(f'Failed to configure PIXCI serial: {self._xclib.pxd_mesgErrorCode(ret)}')
                    return 1

                self._xclib.pxd_serialFlush(1, 0, 1, 1)

                # Configure our custom settings
                self._serial_command('REBOOT', timeout=30)
                self._serial_command('SOC 30HZ_1MSEXP_-40C_LOWNOISE')
                self._serial_command('DATA:STAMP ON')

                self._cooler_setpoint = self._config.cooler_setpoint
                self._serial_command(f'TEMP:SENS:SET {self._cooler_setpoint}')
                self._serial_command('TEC:EN ON')

                self._readout_width = self._xclib.pxd_imageXdim()
                self._readout_height = self._xclib.pxd_imageYdim()

                # Disable frames until we are ready to start exposing
                self._serial_command('SENS:TRIG OFF')

                # Query info for fits headers
                model = self._serial_command('SYS:MODEL?')
                partnumber = self._serial_command('SYS:PN?')
                version = self._serial_command('SYS:VER?')
                self._camera_model = f'{model} ({partnumber}{version})'
                self._camera_serial = self._serial_command('SYS:SN?')
                self._camera_firmware = self._serial_command('SYS:FW?')
                self._camera_software = self._serial_command('SYS:SW?')

                initialized = True

                return CommandStatus.Succeeded
            except Exception:
                traceback.print_exc(file=sys.stdout)
                return CommandStatus.Failed
            finally:
                # Clean up on failure
                if not initialized:
                    if self._xclib is not None:
                        self._xclib.pxd_PIXCIclose()
                        self._xclib = None

                    log.error(self._config.log_name, 'Failed to initialize camera')
                else:
                    log.info(self._config.log_name, 'Initialized camera')

    def set_target_temperature(self, temperature, quiet):
        """Set the target camera temperature"""
        if temperature is not None and (temperature < -65 or temperature > 25):
            return CommandStatus.TemperatureOutsideLimits

        try:
            with self._lock:
                if temperature is not None:
                    self._serial_command(f'TEMP:SENS:SET {self._cooler_setpoint}')
                    self._serial_command('TEC:EN ON')
                else:
                    self._serial_command('TEC:EN OFF')
        except:
            return CommandStatus.Failed

        self._cooler_setpoint = temperature
        if not quiet:
            if temperature is None:
                temperature = 'warm'
            log.info(self._config.log_name, f'Target temperature set to {temperature}')

        return CommandStatus.Succeeded

    def set_exposure(self, exposure, quiet):
        """Set the camera exposure time"""
        if self.is_acquiring:
            return CommandStatus.CameraNotIdle

        self._exposure_time = exposure
        if not quiet:
            log.info(self._config.log_name, f'Exposure time set to {exposure:.3f}s')

        return CommandStatus.Succeeded

    @Pyro4.expose
    def start_sequence(self, count, quiet):
        """Starts an exposure sequence with a set number of frames, or 0 to run until stopped"""
        if self.is_acquiring:
            return CommandStatus.CameraNotIdle

        self._sequence_frame_limit = count
        self._sequence_frame_count = 0
        self._stop_acquisition = False
        self._processing_stop_signal.value = False

        self._acquisition_thread = threading.Thread(
            target=self.__run_exposure_sequence,
            args=(quiet,), daemon=True)
        self._acquisition_thread.start()

        if not quiet:
            count_msg = 'until stopped'
            if count == 1:
                count_msg = '1 frame'
            elif count > 1:
                count_msg = f'{count} frames'

            log.info(self._config.log_name, f'Starting exposure sequence ({count_msg})')

        return CommandStatus.Succeeded

    @Pyro4.expose
    def stop_sequence(self, quiet):
        """Stops any active exposure sequence"""
        if not self.is_acquiring or self._stop_acquisition:
            return CommandStatus.CameraNotAcquiring

        if not quiet:
            log.info(self._config.log_name, 'Aborting exposure sequence')

        self._sequence_frame_count = 0
        self._stop_acquisition = True

        return CommandStatus.Succeeded

    def report_status(self):
        """Returns a dictionary containing the current camera state"""
        # Estimate the current frame progress based on the time delta
        exposure_progress = 0
        sequence_frame_count = self._sequence_frame_count
        state = CameraStatus.Idle

        if self.is_acquiring:
            state = CameraStatus.Acquiring
            if self._stop_acquisition:
                state = CameraStatus.Aborting
            else:
                if self._sequence_exposure_start_time is not None:
                    exposure_progress = (Time.now() - self._sequence_exposure_start_time).to(u.s).value
                    if exposure_progress >= self._exposure_time:
                        state = CameraStatus.Reading

        return {
            'state': state,
            'cooler_mode': self._cooler_mode,
            'cooler_setpoint': self._cooler_setpoint,
            'cooler_voltage': self._cooler_voltage,
            'sensor_temperature': self._sensor_temperature,
            'case_temperature': self._case_temperature,
            'senpcb_temperature': self._senspcb_temperature,
            'digpcb_temperature': self._digpcb_temperature,
            'temperature_locked': self._cooler_mode == CoolerMode.Locked,  # used by opsd
            'exposure_time': self._exposure_time,
            'exposure_progress': exposure_progress,
            'sequence_frame_limit': self._sequence_frame_limit,
            'sequence_frame_count': sequence_frame_count,
        }

    def shutdown(self):
        """Disconnects from the camera driver"""
        # Complete the current exposure
        if self._acquisition_thread is not None:
            print('shutdown: waiting for acquisition to complete')
            self._stop_acquisition = True
            self._acquisition_thread.join()

        with self._lock:
            print('shutdown: disconnecting driver')
            self._xclib.pxd_PIXCIclose()
            self._xclib = None

        log.info(self._config.log_name, 'Shutdown camera')
        return CommandStatus.Succeeded


def scicam_process(camd_pipe, config, processing_queue, processing_framebuffer, processing_framebuffer_offsets,
                   stop_signal):
    cam = SciCamInterface(config, processing_queue, processing_framebuffer, processing_framebuffer_offsets, stop_signal)
    ret = cam.initialize()

    if ret == CommandStatus.Succeeded:
        cam.update_cooler()

    camd_pipe.send(ret)
    if ret != CommandStatus.Succeeded:
        return

    try:
        last_cooler_update = Time.now()
        while True:
            temperature_dirty = False
            if camd_pipe.poll(timeout=1):
                c = camd_pipe.recv()
                command = c['command']
                args = c['args']

                if command == 'temperature':
                    temperature_dirty = True
                    camd_pipe.send(cam.set_target_temperature(args['temperature'], args['quiet']))
                elif command == 'exposure':
                    camd_pipe.send(cam.set_exposure(args['exposure'], args['quiet']))
                elif command == 'start':
                    camd_pipe.send(cam.start_sequence(args['count'], args['quiet']))
                elif command == 'stop':
                    camd_pipe.send(cam.stop_sequence(args['quiet']))
                elif command == 'status':
                    camd_pipe.send(cam.report_status())
                elif command == 'shutdown':
                    break
                else:
                    print(f'unhandled command: {command}')
                    camd_pipe.send(CommandStatus.Failed)

            dt = Time.now() - last_cooler_update
            if temperature_dirty or dt > config.cooler_update_delay * u.s:
                cam.update_cooler()
                last_cooler_update = Time.now()
    except Exception:
        traceback.print_exc(file=sys.stdout)
        camd_pipe.send(CommandStatus.Failed)

    camd_pipe.close()
    cam.shutdown()
