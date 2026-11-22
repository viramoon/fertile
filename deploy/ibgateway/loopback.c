// Linux Gateway's JVM must bind the paper API to IPv4 loopback, before listen().
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <sys/socket.h>

int bind(int fd, const struct sockaddr *addr, socklen_t length) {
    int (*system_bind)(int, const struct sockaddr *, socklen_t) = dlsym(RTLD_NEXT, "bind");
    if (!system_bind) { errno = ENOSYS; return -1; }
    if (addr && addr->sa_family == AF_INET && length >= sizeof(struct sockaddr_in)) {
        struct sockaddr_in local = *(const struct sockaddr_in *)addr;
        if (ntohs(local.sin_port) == 4002) {
            local.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
            return system_bind(fd, (struct sockaddr *)&local, sizeof(local));
        }
        if (ntohs(local.sin_port) == 4001) { errno = EACCES; return -1; }
    } else if (addr && addr->sa_family == AF_INET6 && length >= sizeof(struct sockaddr_in6)) {
        unsigned port = ntohs(((const struct sockaddr_in6 *)addr)->sin6_port);
        if (port == 4001 || port == 4002) { errno = EACCES; return -1; }
    }
    return system_bind(fd, addr, length);
}
