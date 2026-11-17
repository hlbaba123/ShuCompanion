"""Small Windows WAV player exposing the actual device playback clock."""
import ctypes
from ctypes import wintypes
import sys
import uuid


class WavPlayer:
    def __init__(self):
        self._alias=None
        self._api=None

    def _send(self,command):
        if sys.platform!='win32':
            raise RuntimeError('WAV playback requires Windows')
        if self._api is None:
            self._api=ctypes.WinDLL('winmm')
            self._api.mciSendStringW.argtypes=[wintypes.LPCWSTR,wintypes.LPWSTR,wintypes.UINT,wintypes.HANDLE]
            self._api.mciSendStringW.restype=wintypes.DWORD
        buffer=ctypes.create_unicode_buffer(256)
        code=self._api.mciSendStringW(command,buffer,len(buffer),None)
        if code:raise RuntimeError('Audio device error '+str(code))
        return buffer.value

    def play(self,path):
        self.stop()
        alias='shu_voice_'+uuid.uuid4().hex
        try:
            self._send(f'open "{path}" type waveaudio alias {alias}')
            self._alias=alias
            self._send(f'set {alias} time format milliseconds')
            self._send(f'play {alias}')
        except (OSError,RuntimeError):
            self.stop()
            raise

    def status(self):
        if self._alias is None:return 0.,False
        position=float(self._send(f'status {self._alias} position'))/1000
        return position,self._send(f'status {self._alias} mode')=='playing'

    def stop(self):
        if self._alias is not None:
            alias,self._alias=self._alias,None
            self._send(f'close {alias}')
