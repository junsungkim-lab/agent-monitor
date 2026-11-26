"""테스트용 가짜 supertonic: 실제 모델 없이 API 모양만 흉내 낸다."""
import os

import numpy as np

CALLS = []


class TTS:
    def __init__(self, model='supertonic-3', auto_download=True):
        if os.environ.get('FAKE_SUPERTONIC_FAIL'):
            raise RuntimeError('download failed')
        self.sample_rate = 24000
        self.voice_style_names = ['M1', 'F2', 'F1', 'M2', 'F3', 'M3', 'M4', 'F4', 'M5', 'F5']

    def get_voice_style(self, voice_name):
        return {'name': voice_name}

    def synthesize(self, text, voice_style, speed=1.05, lang=None, **kw):
        CALLS.append((text, voice_style['name'], speed, lang))
        n = int(self.sample_rate * 0.25)
        wav = 0.5 * np.sin(np.linspace(0, 440 * 2 * np.pi * 0.25, n, dtype=np.float32))
        return wav.reshape(1, -1), np.array([0.25])
