# Lab speech fixture

`speech_mask_ljspeech_lj001-0001_8k.wav` is a single human-read speech clip from LJSpeech 1.1, sample
`LJ001-0001.wav`, recorded by LibriVox volunteer Linda Johnson. The LJSpeech dataset describes the recordings as
public domain. The source is mono 16-bit PCM at 22.05 kHz; this fixture was resampled to mono 16-bit PCM at 8 kHz
for the lab's PCMU RTP path and raised by 1 dB during conversion.

Source clip: [LJSpeech sample on Hugging Face](https://huggingface.co/datasets/flexthink/ljspeech/resolve/main/wavs/LJ001-0001.wav).
Dataset description and public-domain status: [audEERING LJSpeech entry](https://audeering.github.io/datasets/datasets/ljspeech.html).

To reproduce the conversion, download the source WAV and run:

```sh
ffmpeg -i LJ001-0001.wav -ar 8000 -ac 1 -sample_fmt s16 -af volume=1dB \
  lab/assets/speech_mask_ljspeech_lj001-0001_8k.wav
```
