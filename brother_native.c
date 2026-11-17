/* On-demand CUPS transport: system libraries only; no interpreter or daemon. */
#include <arpa/inet.h>
#include <assert.h>
#include <cups/cups.h>
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <ifaddrs.h>
#include <net/if.h>
#include <pthread.h>
#include <regex.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/file.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#if !defined(PRINTER_SERIAL) || !defined(PRINTER_SEED_IP)
#error "Build with install_connection.py --build-native to load printer.json"
#endif
#define URI "brotherauto://" PRINTER_SERIAL
#define CACHE "/Library/Caches/BrotherAutoConnection/"
#define LIMIT 262144
#define MAX_IPS 2048
#define WORKERS 24

static double now(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

static int ready(int fd, int writing, double deadline) {
    for (;;) {
        double remaining = deadline - now();
        if (remaining <= 0) return 0;
        struct timeval tv = {(time_t)remaining, (suseconds_t)((remaining - (time_t)remaining) * 1e6)};
        fd_set set;
        FD_ZERO(&set); FD_SET(fd, &set);
        int rc = select(fd + 1, writing ? NULL : &set, writing ? &set : NULL, NULL, &tv);
        if (rc < 0 && errno == EINTR) continue;
        return rc > 0;
    }
}

static int connect_ip(uint32_t ip, unsigned port, double timeout) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0 || fd >= FD_SETSIZE) { if (fd >= 0) close(fd); return -1; }
    int on = 1;
    setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &on, sizeof(on));
    if (fcntl(fd, F_SETFL, O_NONBLOCK) < 0) { close(fd); return -1; }
    struct sockaddr_in address = {.sin_len = sizeof(address), .sin_family = AF_INET,
                                 .sin_port = htons((uint16_t)port), .sin_addr.s_addr = htonl(ip)};
    int rc = connect(fd, (struct sockaddr *)&address, sizeof(address));
    if (rc < 0 && errno != EINPROGRESS) { close(fd); return -1; }
    if (rc < 0) {
        int error = 0; socklen_t size = sizeof(error);
        if (!ready(fd, 1, now() + timeout) || getsockopt(fd, SOL_SOCKET, SO_ERROR, &error, &size) || error) {
            close(fd); return -1;
        }
    }
    return fd;
}

static int field_is(const char *body, const char *label_pattern, const char *expected) {
    char pattern[400];
    snprintf(pattern, sizeof(pattern), "%s[[:space:]]*</dt>[[:space:]]*<dd>[[:space:]]*([^<]*)</dd>", label_pattern);
    regex_t regex;
    if (regcomp(&regex, pattern, REG_EXTENDED | REG_ICASE)) return 0;
    regmatch_t match[3];
    int ok = regexec(&regex, body, 3, match, 0) == 0;
    regfree(&regex);
    if (!ok || match[2].rm_so < 0) return 0;
    const char *start = body + match[2].rm_so;
    size_t length = (size_t)(match[2].rm_eo - match[2].rm_so);
    while (length && isspace((unsigned char)start[length - 1])) length--;
    return strlen(expected) == length && memcmp(start, expected, length) == 0;
}

static int identity_matches(const char *body) {
    return (field_is(body, "Model(&#32;|&nbsp;|[[:space:]])+Name", "Brother HL-1210W series") ||
            field_is(body, "Model(&#32;|&nbsp;|[[:space:]])+Name", "Brother HL-1210W")) &&
            field_is(body, "Serial(&#32;|&nbsp;|[[:space:]])+no[.]", PRINTER_SERIAL);
}

/* Decode one complete HTTP response in place, including chunked bodies. */
static char *http_body(char *response, size_t size) {
    response[size] = 0;
    if (strncmp(response, "HTTP/1.0 200 ", 13) && strncmp(response, "HTTP/1.1 200 ", 13)) return NULL;
    char *separator = strstr(response, "\r\n\r\n");
    if (!separator) return NULL;
    char *body = separator + 4;
    size_t body_size = size - (size_t)(body - response);
    *separator = 0;
    char *transfer = strcasestr(response, "\r\nTransfer-Encoding:");
    char *length = strcasestr(response, "\r\nContent-Length:");
    if (transfer) {
        char *line_end = strstr(transfer + 2, "\r\n");
        char *chunked = strcasestr(transfer, "chunked");
        if (!chunked || (line_end && chunked > line_end)) return NULL;
        char *input = body, *output = body, *end = body + body_size;
        for (;;) {
            char *next = strstr(input, "\r\n"), *parsed = NULL;
            if (!next || next >= end || !isxdigit((unsigned char)*input)) return NULL;
            errno = 0;
            unsigned long amount = strtoul(input, &parsed, 16);
            if (errno || (parsed != next && *parsed != ';') || amount > LIMIT) return NULL;
            input = next + 2;
            if (amount == 0) { *output = 0; return body; }
            if ((size_t)(end - input) < amount + 2 || input[amount] != '\r' || input[amount + 1] != '\n') return NULL;
            memmove(output, input, amount);
            output += amount; input += amount + 2;
        }
    }
    if (length) {
        char *parsed;
        errno = 0;
        unsigned long expected = strtoul(length + strlen("\r\nContent-Length:"), &parsed, 10);
        if (errno || expected > body_size || parsed == length + strlen("\r\nContent-Length:")) return NULL;
        body[expected] = 0;
    }
    return body;
}

static int identify(uint32_t ip) {
    int fd = connect_ip(ip, 631, 0.5);
    if (fd < 0) return 0;
    close(fd);
    fd = connect_ip(ip, 80, 0.5);
    if (fd < 0) return 0;
    char address[INET_ADDRSTRLEN], request[300];
    struct in_addr binary = {.s_addr = htonl(ip)};
    inet_ntop(AF_INET, &binary, address, sizeof(address));
    int count = snprintf(request, sizeof(request), "GET /general/information.html?kind=item HTTP/1.0\r\nHost: %s\r\nConnection: close\r\n\r\n", address);
    double deadline = now() + 3.0;
    size_t sent = 0;
    while (sent < (size_t)count) {
        if (!ready(fd, 1, deadline)) { close(fd); return 0; }
        ssize_t n = send(fd, request + sent, (size_t)count - sent, 0);
        if (n < 0 && (errno == EAGAIN || errno == EINTR)) continue;
        if (n <= 0) { close(fd); return 0; }
        sent += (size_t)n;
    }
    char *response = malloc(LIMIT + 1);
    if (!response) { close(fd); return 0; }
    size_t size = 0;
    while (size < LIMIT && ready(fd, 0, deadline)) {
        ssize_t n = recv(fd, response + size, LIMIT - size, 0);
        if (n < 0 && (errno == EAGAIN || errno == EINTR)) continue;
        if (n <= 0) break;
        size += (size_t)n;
    }
    close(fd);
    char *body = size < LIMIT ? http_body(response, size) : NULL;
    int matches = body && identity_matches(body);
    free(response);
    return matches;
}

static uint32_t ips[MAX_IPS], local_ips[32], found_ip;
static size_t ip_count, local_count, next_ip;
static pthread_mutex_t mutex = PTHREAD_MUTEX_INITIALIZER;

static int private_ip(uint32_t ip) {
    return (ip >> 24) == 10 || (ip >> 20) == 0xac1 || (ip >> 16) == 0xc0a8;
}

static int collect_networks(void) {
    struct ifaddrs *head;
    if (getifaddrs(&head)) return 0;
    for (struct ifaddrs *node = head; node; node = node->ifa_next) {
        if (!node->ifa_addr || !node->ifa_netmask || node->ifa_addr->sa_family != AF_INET ||
            strncmp(node->ifa_name, "en", 2) || !isdigit((unsigned char)node->ifa_name[2]) ||
            !(node->ifa_flags & IFF_UP)) continue;
        uint32_t ip = ntohl(((struct sockaddr_in *)node->ifa_addr)->sin_addr.s_addr);
        uint32_t mask = ntohl(((struct sockaddr_in *)node->ifa_netmask)->sin_addr.s_addr);
        uint32_t base = ip & mask, hosts = ~mask;
        if (hosts > 1023 || hosts < 3 || (hosts & (hosts + 1)) ||
            !private_ip(base) || !private_ip(base + hosts)) continue;
        if (local_count < 32) local_ips[local_count++] = ip;
        for (uint32_t offset = 1; offset < hosts; offset++) {
            uint32_t candidate = base + offset;
            int duplicate = 0;
            for (size_t j = 0; j < ip_count; j++) if (ips[j] == candidate) { duplicate = 1; break; }
            if (!duplicate) {
                if (ip_count == MAX_IPS) { freeifaddrs(head); return 0; }
                ips[ip_count++] = candidate;
            }
        }
    }
    freeifaddrs(head);
    return ip_count > 0;
}

static int allowed_ip(uint32_t ip) {
    for (size_t j = 0; j < local_count; j++) if (local_ips[j] == ip) return 0;
    for (size_t j = 0; j < ip_count; j++) if (ips[j] == ip) return 1;
    return 0;
}

static void *worker(void *unused) {
    (void)unused;
    for (;;) {
        pthread_mutex_lock(&mutex);
        if (found_ip || next_ip == ip_count) { pthread_mutex_unlock(&mutex); break; }
        uint32_t ip = ips[next_ip++];
        pthread_mutex_unlock(&mutex);
        if (allowed_ip(ip) && identify(ip)) {
            pthread_mutex_lock(&mutex); found_ip = ip; pthread_mutex_unlock(&mutex);
            break;
        }
    }
    return NULL;
}

static uint32_t parse_ip(const char *text) {
    struct in_addr address;
    return inet_pton(AF_INET, text, &address) == 1 ? ntohl(address.s_addr) : 0;
}

static uint32_t find_printer(int force_scan) {
    if (!collect_networks()) return 0;
    char last[64] = "";
    int fd = open(CACHE "last-ip", O_RDONLY | O_NOFOLLOW);
    if (fd >= 0) { ssize_t n = read(fd, last, sizeof(last) - 1); if (n > 0) last[n] = 0; close(fd); }
    uint32_t cached = parse_ip(last), seed = parse_ip(PRINTER_SEED_IP);
    if (!force_scan && allowed_ip(cached) && identify(cached)) return cached;
    if (!force_scan && seed != cached && allowed_ip(seed) && identify(seed)) return seed;
    pthread_t threads[WORKERS]; size_t started = 0;
    for (; started < WORKERS; started++) if (pthread_create(&threads[started], NULL, worker, NULL)) break;
    if (!started) worker(NULL);
    for (size_t j = 0; j < started; j++) pthread_join(threads[j], NULL);
    return found_ip;
}

static int write_cache(const char *name, const char *data) {
    char path[512];
    snprintf(path, sizeof(path), CACHE "%s.tmp", name);
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW, 0600);
    if (fd < 0) return 0;
    size_t left = strlen(data), offset = 0;
    while (left) {
        ssize_t written = write(fd, data + offset, left);
        if (written < 0 && errno == EINTR) continue;
        if (written <= 0) { close(fd); return 0; }
        offset += (size_t)written; left -= (size_t)written;
    }
    if (close(fd)) return 0;
    char destination[512]; snprintf(destination, sizeof(destination), CACHE "%s", name);
    return rename(path, destination) == 0;
}

static int self_test(void) {
    assert(identity_matches("<dt>Model&#32;Name</dt><dd>Brother HL-1210W series</dd><dt>Serial&#32;no.</dt><dd>" PRINTER_SERIAL "</dd>"));
    assert(!identity_matches("<dt>Model Name</dt><dd>Brother HL-1210W series</dd><dt>Serial no.</dt><dd>" PRINTER_SERIAL "X</dd>"));
    assert(!identity_matches("<dt>Model Name</dt><dd>Other Printer</dd><dt>Serial no.</dt><dd>" PRINTER_SERIAL "</dd>"));
    char response[] = "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n2\r\nde\r\n0\r\n\r\n";
    char *body = http_body(response, strlen(response)); assert(body && !strcmp(body, "abcde"));
    char truncated[] = "HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\nshort";
    assert(http_body(truncated, strlen(truncated)) == NULL);
    char rejected[] = "HTTP/1.1 403 Forbidden\r\n\r\n";
    assert(http_body(rejected, strlen(rejected)) == NULL);
    assert(private_ip(parse_ip("10.123.45.67")) && !private_ip(parse_ip("192.0.2.1")));
    cups_option_t *options = NULL;
    int count = cupsParseOptions("brother-connection-check-only=true media=A4", 0, &options);
    assert(!strcmp(cupsGetOption("brother-connection-check-only", count, options), "true"));
    cupsFreeOptions(count, options);
    puts("Native backend tests passed (identity, HTTP framing, local-address policy, diagnostic option).");
    return 0;
}

int main(int argc, char **argv) {
    signal(SIGPIPE, SIG_IGN);
    if (argc == 1) { puts("network brotherauto \"Brother HL-1210W\" \"Brother HL-1210W (on-demand discovery)\""); return 0; }
    if (argc == 2 && !strcmp(argv[1], "--identity")) { puts(PRINTER_SERIAL); return 0; }
    if (argc == 2 && !strcmp(argv[1], "--self-test")) return self_test();
    int diagnostic = (argc == 2 || argc == 3) && !strcmp(argv[1], "--diagnose");
    if (diagnostic && argc == 3 && strcmp(argv[2], "--scan")) return 4;
    if (!diagnostic) {
        if (argc != 6 && argc != 7) { fputs("ERROR: Invalid CUPS arguments.\n", stderr); return 4; }
        if (!*argv[1] || strlen(argv[1]) > 16) return 4;
        for (const char *c = argv[1]; *c; c++) if (!isdigit((unsigned char)*c)) return 4;
        const char *uri = getenv("DEVICE_URI");
        if (!uri || strcmp(uri, URI)) { fputs("ERROR: Printer identity mismatch.\n", stderr); return 4; }
    }
    int lock = -1;
    if (geteuid() == 0) {
        lock = open(CACHE "discovery.lock", O_CREAT | O_RDWR | O_NOFOLLOW, 0600);
        if (lock < 0 || flock(lock, LOCK_EX)) { fputs("WARNING: Cannot access discovery cache.\n", stderr); return 6; }
    }
    uint32_t ip = find_printer(diagnostic && argc == 3);
    if (!ip) { fputs("WARNING: Exact Brother printer not found; retaining the print job for retry.\n", stderr); if (lock >= 0) close(lock); return 6; }
    char address[INET_ADDRSTRLEN], uri[80], record[512];
    struct in_addr binary = {.s_addr = htonl(ip)};
    inet_ntop(AF_INET, &binary, address, sizeof(address));
    snprintf(uri, sizeof(uri), "ipp://%s/", address);
    cups_option_t *options = NULL;
    int option_count = diagnostic ? 0 : cupsParseOptions(argv[5], 0, &options);
    const char *check_option = cupsGetOption("brother-connection-check-only", option_count, options);
    int check_only = diagnostic || (check_option && !strcmp(check_option, "true"));
    cupsFreeOptions(option_count, options);
    snprintf(record, sizeof(record), "{\"ip\":\"%s\",\"serial\":\"%s\",\"resolved_uri\":\"%s\",\"job_id\":\"%s\",\"mode\":\"%s\",\"observed_epoch\":%lld}\n",
             address, PRINTER_SERIAL, uri, diagnostic ? "diagnostic" : argv[1], check_only ? "connection-check-only" : "print", (long long)time(NULL));
    if (geteuid() == 0 && (!write_cache("last-ip", address) || !write_cache("last-seen.json", record))) {
        fputs("WARNING: Could not save verified printer address.\n", stderr); if (lock >= 0) close(lock); return 6;
    }
    if (lock >= 0) close(lock);
    if (check_only) { fputs(record, stdout); fputs("INFO: Verified Brother connection; sent no print data.\n", stderr); return 0; }
    fprintf(stderr, "INFO: Verified Brother HL-1210W at %s; sending via native IPP.\n", address);
    if (setenv("DEVICE_URI", uri, 1)) return 6;
    argv[0] = uri;
    execv("/usr/libexec/cups/backend/ipp", argv);
    fprintf(stderr, "ERROR: Cannot start native IPP backend: %s\n", strerror(errno));
    return 4;
}
