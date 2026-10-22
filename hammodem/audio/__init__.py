"""Audio legs: where the modem's samples go and come from (UDP raw PCM now; RTP/SIP and IAX2 next).

Interface: ``fds()``; ``service(readable) -> list[np.ndarray]`` (received int16 blocks); ``send(pcm)``;
``frame_samples`` (samples per outgoing block); ``name``; ``close()``.
"""
