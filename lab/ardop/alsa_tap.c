/* LD_PRELOAD shim for ardopcf in the lab: tap its playback samples straight into a FIFO.
 *
 * ardopcf plays into a plain "null" PCM (no hardware). Every frame ALSA accepts from snd_pcm_writei is also
 * written, unbuffered, to /tmp/ardop_tx.fifo, which relay.py paces out in real time. (ALSA's file plugin is no
 * good here: it always holds back one buffer and flushes it only on drain/close, which ardopcf does not do between
 * frames, so the end of every frame arrived late.) ardopcf times PTT itself from the number of samples it wrote.
 *
 * gcc -shared -fPIC -o alsa_tap.so alsa_tap.c -ldl -lasound
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <unistd.h>
#include <alsa/asoundlib.h>

snd_pcm_sframes_t snd_pcm_writei(snd_pcm_t *pcm, const void *buffer, snd_pcm_uframes_t size)
{
	static snd_pcm_sframes_t (*real)(snd_pcm_t *, const void *, snd_pcm_uframes_t);
	static int fd = -1;
	if (!real)
		real = (snd_pcm_sframes_t (*)(snd_pcm_t *, const void *, snd_pcm_uframes_t))dlsym(RTLD_NEXT, "snd_pcm_writei");
	snd_pcm_sframes_t ret = real(pcm, buffer, size);
	if (ret > 0 && snd_pcm_stream(pcm) == SND_PCM_STREAM_PLAYBACK) {
		if (fd < 0)
			fd = open("/tmp/ardop_tx.fifo", O_WRONLY);     /* relay.py holds it open O_RDWR: never blocks */
		if (fd >= 0) {
			const char *p = buffer;
			ssize_t left = snd_pcm_frames_to_bytes(pcm, ret);
			while (left > 0) {
				ssize_t n = write(fd, p, left);
				if (n <= 0)
					break;
				p += n;
				left -= n;
			}
		}
	}
	return ret;
}
