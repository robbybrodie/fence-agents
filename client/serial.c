/* 
 * Copyright (C) 2002-2003, 2009 Red Hat, Inc.
 *
 * License: GPL.
 *
 * Written by Lon Hohberger <lhh@redhat.com>
 *
 * Ubersimpledumbterminal "ser" version 1.0.3
 */

#include <stdio.h>
#include <termios.h>
#include <unistd.h>
#include <stdlib.h>
#include <sys/select.h>
#include <fcntl.h>
#include <errno.h>
#include <string.h>
#include <sys/ioctl.h>
#include <fdops.h>
#include <xvm.h>
#include <options.h>
#include <client.h>


static int
char_to_speed(const char *speed)
{
	if (!speed || !strlen(speed))
		return B9600;
	if (!strcmp(speed,"2400"))
		return B2400;
	if (!strcmp(speed,"9600"))
		return B9600;
	if (!strcmp(speed,"19200"))
		return B19200;
	if (!strcmp(speed,"38400"))
		return B38400;
	if (!strcmp(speed,"57600"))
		return B57600;
	if (!strcmp(speed,"115200"))
		return B115200;
	return -1;
}


static int
char_to_flags(const char *param)
{
	int db_f = CS8, par_f = 0, sb_f = 0, x;

	if (!param || !strlen(param))
		return (db_f | par_f | sb_f);

	if (strlen(param) < 3) {
		errno = EINVAL;
		return -1;
	}

	for (x = 0; x < 3; x++) {
		switch (param[x]) {
		case '5':
			db_f = CS5;
			break;
		case '6':
			db_f = CS6;
			break;
		case '7':
			db_f = CS7;
			break;
		case '8':
			db_f = CS8;
			break;
		case 'n':
		case 'N':
			par_f = 0;
			break;
		case 'e':
		case 'E':
			par_f = PARENB;
			break;
		case 'o':
		case 'O':
			par_f = PARENB | PARODD;
			break;
		case '1':
			sb_f = 0;
			break;
		case '2':
			sb_f = CSTOPB;
			break;
		default:
			printf("Fail: %c\n", param[x]);
			errno = EINVAL;
			return -1;
		}
	}

	return (db_f | par_f | sb_f);
}


static int
open_port(char *file, char *cspeed, char *cparam)
{
	struct termios  ti;
	int fd, speed = B115200, flags = 0;
	struct flock lock;

	if ((speed = char_to_speed(cspeed)) == -1) {
		errno = EINVAL;
		return -1;
	}

	if ((flags = char_to_flags(cparam)) == -1) {
		errno = EINVAL;
		return -1;
	}

	if ((fd = open(file, O_RDWR | O_EXCL)) == -1) {
		perror("open");
		return -1;
	}

	memset(&lock,0,sizeof(lock));
	lock.l_type = F_WRLCK;
	if (fcntl(fd, F_SETLK, &lock) == -1) {
		perror("Failed to lock serial port");
		close(fd);
		return -1;
	}

	memset(&ti, 0, sizeof(ti));
	ti.c_cflag = (speed | CLOCAL | CRTSCTS | CREAD | flags);

	if (tcsetattr(fd, TCSANOW, &ti) < 0) {
		perror("tcsetattr");
		close(fd);
		return -1;
	}                             

	(void) tcflush(fd, TCIOFLUSH);

	return fd;
}


static void
hangup(int fd, int delay)
{
	unsigned int bits;

	if (ioctl(fd, TIOCMGET, &bits)) {
		perror("ioctl1");
		return;
	}

	bits &= ~(TIOCM_DTR | TIOCM_CTS | TIOCM_RTS | TIOCM_DSR | TIOCM_CD);

	if (ioctl(fd, TIOCMSET, &bits)) {
		perror("ioctl2");
		return;
	}
	
	usleep(delay);

	bits |= (TIOCM_DTR | TIOCM_CTS | TIOCM_RTS | TIOCM_DSR | TIOCM_CD);

	if (ioctl(fd, TIOCMSET, &bits)) {
		perror("ioctl3");
		return;
	}
}


int
serial_fence_virt(fence_virt_args_t *args)
{
	serial_req_t req;
	int fd, ret;
	char speed[32], *flags = NULL;
	struct timeval tv;
	serial_resp_t resp;

	strncpy(speed, args->serial.speed, sizeof(speed));

	printf("Port: %s Speed: %s\n", args->serial.device, speed);

	if ((flags = strchr(speed, ','))) {
		*flags = 0;
		flags++;
	}


	fd = open_port(args->serial.device, speed, flags);
	if (fd == -1) {
		perror("open_port");
		return -1;
	}

	hangup(fd, 300000);

	memset(&req, 0, sizeof(req));
	req.magic = SERIAL_MAGIC;
	req.request = (uint8_t)args->op;
	if (args->flags & RF_UUID)
		req.flags |= RF_UUID;
	strncpy((char *)req.domain, args->domain, sizeof(req.domain));

	
	tv.tv_sec = 3;
	tv.tv_usec = 0;
	ret = _write_retry(fd, &req, sizeof(req), &tv);
	if (ret < sizeof(req)) {
		if (ret < 0)
			return ret;
		printf("Failed to send request\n");
	}

	tv.tv_sec = args->timeout;
	tv.tv_usec = 0;
	do {
		memset(&resp, 0, sizeof(resp));
		ret = _read_retry(fd, &resp, sizeof(resp), &tv);
	} while(resp.magic != SERIAL_MAGIC && (tv.tv_sec || tv.tv_usec));

	if (resp.magic != SERIAL_MAGIC)
		return -1;
	ret = (int)resp.response;

	/* XXX try a response from netcat: e.g.
	   abba1<cr> = fail; abba0<cr>=ok
	   should be removed when we have "real" 
	   server side handling; this was just to
	   test communications */
	if (ret & 0x30) 
		ret &= (~0x30);

	close(fd);

	printf("Response: %d\n", ret);

	return ret;
}
