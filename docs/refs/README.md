# Reference audio for the HF keyboard modes

These are publicly available over-the-air recordings used to measure and test
the PACTOR, Clover and G-TOR implementations.  They are signal samples only;
no vendor software, firmware or source code is included.  The recordings remain
the property of their originators and are kept here for interoperability
research and regression testing.

| File | Mode | Source |
|---|---|---|
| `pactor2_arq.wav` | PACTOR-II ARQ | Astrosurf / Wayback archive |
| `pactor2.mp3` | PACTOR-II | Astrosurf / Wayback archive |
| `pactor3.wav` | PACTOR-III | Astrosurf / Wayback archive |
| `clover_ii.wav` | Clover-II | Astrosurf / Wayback archive |
| `clover2000_qpsm.wav` | Clover-2000 QPSM | Astrosurf / Wayback archive |
| `gtor_astrosurf.wav` | G-TOR | Astrosurf / Wayback archive |
| `pactor.wav` | PACTOR-I | qsl.net/ws1sm |
| `clover.mp3` | Clover | qsl.net/ws1sm |
| `gtor.mp3` | G-TOR | qsl.net/ws1sm |

## Checksums

Verified with `sha256sum` on 2026-10-23:

```
4af772aaf6e228942b108476a0a7d41c89a6700d387a9e6eb3b3ad2ab1b678ca  clover2000_qpsm.wav
70aff12c96034ab1a72a504897f58b8c9a387c9166fc272552e16dd5cb73b45d  clover_ii.wav
d4c3be40982c08cc0b875756ff4b5643050e38b54e0d84968565f1e8c0858a12  clover.mp3
041b0094ed1767b72d94737dc55285388b710e47aa39e3e3004a07ccaa03d36f  gtor_astrosurf.wav
659e9daee137689fb63d41e5a10a08b56c93150e9a22aa19c6ba7174d84b2fea  gtor.mp3
a5ee0d0fcb94af7cae83cbac0e7cd09e0e5f52c1b0cb252fd2fc16c2db979cfb  pactor2_arq.wav
c18ee6fa40d35393cd71f015386cb89835b70df3597fe4b241664e1259f24d9f  pactor2.mp3
18182991dbbeb601698092add4b5461bc6292f3c976a0e5a59c8ede06a45ee2c  pactor3.wav
a6cf448060e3ad5e8417b916bcc3d19a3e555899b1a26f21e8064fe11367aa05  pactor.wav
```

The WAV filenames are exempted from the repository-wide `*.wav` ignore rule in
`.gitignore`; the MP3 references were already tracked by filename.
