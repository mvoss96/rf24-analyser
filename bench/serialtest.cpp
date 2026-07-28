// The same measurements as bench/serialtest.py, with Python taken out of the way.
//
// pyserial reaches 100 % of the line reading and 89-91 % writing, and the
// writing side is the one that carries a transfer. Before accepting that as the
// hardware's answer, it is worth asking whether it is Python's: every write goes
// through the interpreter, and a 34-byte record leaves at 20 us a byte, so there
// is room for an overhead of a few hundred microseconds to hide in.
//
// This talks to the same firmware (bench/serialbench) over the same port with
// the raw Win32 calls pyserial is a wrapper around, so the two are directly
// comparable. It also does one thing pyserial cannot: keep several writes
// outstanding at once, so the driver never has to come back and ask for more.
//
// Build:  g++ -O2 -o bench/serialtest.exe bench/serialtest.cpp
// Run:    bench/serialtest.exe COM18 [baud]

#include <windows.h>

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

// Matching serialtest.py: one length byte, 32 payload bytes, one crc.
static const int RECORD = 34;
static const int WINDOW = 7;
static const int CONFIRM = 4;
static const int BITS_PER_BYTE = 10;   // 8N1

static double g_freq = 0.0;

static double now_ms() {
    LARGE_INTEGER c;
    QueryPerformanceCounter(&c);
    return c.QuadPart * 1000.0 / g_freq;
}

static double wire_ms(long long nbytes, int baud) {
    return nbytes * BITS_PER_BYTE * 1000.0 / baud;
}

// ------------------------------------------------------------------- the link

struct Link {
    HANDLE h = INVALID_HANDLE_VALUE;
    HANDLE ev = nullptr;
    std::string buf;
};

// One overlapped operation, waited out. The port is opened overlapped so that
// the multi-write test below is possible at all; everything else wants ordinary
// blocking behaviour, which is what this restores.
static DWORD io(Link& L, bool write, void* data, DWORD n, DWORD timeout_ms) {
    OVERLAPPED ov;
    ZeroMemory(&ov, sizeof(ov));
    ov.hEvent = L.ev;
    ResetEvent(L.ev);

    DWORD done = 0;
    BOOL ok = write ? WriteFile(L.h, data, n, &done, &ov)
                    : ReadFile(L.h, data, n, &done, &ov);
    if (!ok) {
        if (GetLastError() != ERROR_IO_PENDING) return 0;
        if (WaitForSingleObject(L.ev, timeout_ms) != WAIT_OBJECT_0) {
            CancelIo(L.h);
            GetOverlappedResult(L.h, &ov, &done, TRUE);
            return done;
        }
        if (!GetOverlappedResult(L.h, &ov, &done, FALSE)) return 0;
    }
    return done;
}

// Whatever has already arrived, or nothing after `timeout_ms` of silence.
//
// This is one call where pyserial needs two: the comm timeouts below say
// "return immediately with what is buffered, but if that is nothing, wait for
// the first byte". Python has to ask for one byte and then ask again how many
// more are waiting. If pyserial were losing anything, this is where it would
// show.
static size_t pump(Link& L, DWORD timeout_ms = 50) {
    COMMTIMEOUTS ct;
    ct.ReadIntervalTimeout = MAXDWORD;
    ct.ReadTotalTimeoutMultiplier = MAXDWORD;
    ct.ReadTotalTimeoutConstant = timeout_ms;
    ct.WriteTotalTimeoutMultiplier = 0;
    ct.WriteTotalTimeoutConstant = 5000;
    SetCommTimeouts(L.h, &ct);

    char tmp[8192];
    DWORD got = io(L, false, tmp, sizeof(tmp), timeout_ms + 1000);
    if (got) L.buf.append(tmp, got);
    return got;
}

static void write_all(Link& L, const void* data, size_t n) {
    const char* p = static_cast<const char*>(data);
    size_t left = n;
    while (left) {
        DWORD done = io(L, true, const_cast<char*>(p), (DWORD)left, 5000);
        if (!done) break;
        p += done;
        left -= done;
    }
}

static void clear(Link& L) {
    L.buf.clear();
    PurgeComm(L.h, PURGE_RXCLEAR | PURGE_TXCLEAR);
}

static std::string line(Link& L, double timeout_s = 2.0) {
    double deadline = now_ms() + timeout_s * 1000.0;
    for (;;) {
        size_t nl = L.buf.find('\n');
        if (nl != std::string::npos) {
            std::string out = L.buf.substr(0, nl);
            L.buf.erase(0, nl + 1);
            while (!out.empty() && (out.back() == '\r' || out.back() == ' ')) out.pop_back();
            return out;
        }
        if (now_ms() >= deadline) return "";
        pump(L);
    }
}

static std::string command(Link& L, const std::string& cmd, double timeout_s = 2.0) {
    clear(L);
    std::string s = cmd + "\n";
    write_all(L, s.data(), s.size());
    return line(L, timeout_s);
}

static long field(const std::string& s, const char* key) {
    std::string want = std::string(key) + "=";
    size_t at = s.find(want);
    if (at == std::string::npos) return 0;
    return strtol(s.c_str() + at + want.size(), nullptr, 10);
}

static bool open_link(Link& L, const char* port, int baud) {
    std::string path = std::string("\\\\.\\") + port;
    L.h = CreateFileA(path.c_str(), GENERIC_READ | GENERIC_WRITE, 0, nullptr,
                      OPEN_EXISTING, FILE_FLAG_OVERLAPPED, nullptr);
    if (L.h == INVALID_HANDLE_VALUE) {
        printf("cannot open %s: error %lu\n", port, GetLastError());
        return false;
    }
    L.ev = CreateEventA(nullptr, TRUE, FALSE, nullptr);

    SetupComm(L.h, 4096, 4096);

    DCB dcb;
    ZeroMemory(&dcb, sizeof(dcb));
    dcb.DCBlength = sizeof(dcb);
    if (!GetCommState(L.h, &dcb)) return false;
    dcb.BaudRate = baud;
    dcb.ByteSize = 8;
    dcb.Parity = NOPARITY;
    dcb.StopBits = ONESTOPBIT;
    dcb.fBinary = TRUE;
    dcb.fParity = FALSE;
    dcb.fOutxCtsFlow = FALSE;
    dcb.fOutxDsrFlow = FALSE;
    dcb.fOutX = FALSE;
    dcb.fInX = FALSE;
    dcb.fRtsControl = RTS_CONTROL_ENABLE;
    // Raising DTR resets the board, exactly as opening the port from Python
    // does. Both are then measuring a dongle that has just booted.
    dcb.fDtrControl = DTR_CONTROL_ENABLE;
    if (!SetCommState(L.h, &dcb)) return false;

    pump(L, 1);   // installs the timeouts
    return true;
}

// -------------------------------------------------------------------- reading

static void test_source(Link& L, int baud, int nbytes) {
    std::string ack = command(L, "s " + std::to_string(nbytes));
    if (ack.rfind("OK src", 0) != 0) { printf("  read   refused: %s\n", ack.c_str()); return; }

    std::string got;
    got.reserve(nbytes);
    double t0 = now_ms();
    double deadline = t0 + std::max(5000.0, wire_ms(nbytes, baud) * 4);
    while ((int)L.buf.size() < nbytes && now_ms() < deadline) pump(L);
    double t1 = now_ms();

    got = L.buf.substr(0, std::min<size_t>(L.buf.size(), nbytes));
    L.buf.erase(0, got.size());

    std::string tail = line(L, 2.0);

    long bad = 0, first = -1;
    for (size_t i = 0; i < got.size(); i++) {
        if ((unsigned char)got[i] != (unsigned char)(i & 0xFF)) {
            if (first < 0) first = (long)i;
            bad++;
        }
    }

    double ms = t1 - t0;
    double kbs = ms > 0 ? got.size() / ms : 0.0;
    printf("  read   %zu B in %.1f ms = %.1f kB/s, %.0f%% of the line "
           "(wire %.0f ms, device says %.0f ms)\n",
           got.size(), ms, kbs, kbs * 1000 * BITS_PER_BYTE / baud * 100,
           wire_ms(nbytes, baud), field(tail, "us") / 1000.0);
    if (bad || (int)got.size() < nbytes)
        printf("         !! first wrong byte at %ld, %ld wrong, %d never arrived\n",
               first, bad, nbytes - (int)got.size());
}

// -------------------------------------------------------------------- writing

static void test_sink(Link& L, int baud, int nbytes, int chunk) {
    std::string ack = command(L, "r");
    if (ack.rfind("OK sink", 0) != 0) { printf("  write  refused: %s\n", ack.c_str()); return; }

    std::vector<char> data(nbytes);
    for (int i = 0; i < nbytes; i++) data[i] = (char)(i & 0xFF);

    double t0 = now_ms();
    for (int off = 0; off < nbytes; off += chunk)
        write_all(L, data.data() + off, std::min(chunk, nbytes - off));
    double t1 = now_ms();

    std::string rep = line(L, 3.0);
    double ms = t1 - t0;
    double kbs = ms > 0 ? nbytes / ms : 0.0;
    printf("  write  %d B in %.1f ms = %.1f kB/s, %.0f%% of the line, in %d B chunks "
           "(device says %.0f ms)\n",
           nbytes, ms, kbs, kbs * 1000 * BITS_PER_BYTE / baud * 100, chunk,
           field(rep, "us") / 1000.0);
    if (field(rep, "bad") || field(rep, "n") != nbytes)
        printf("         !! %ld wrong bytes, %ld never arrived\n",
               field(rep, "bad"), nbytes - field(rep, "n"));
}

// The one thing Python cannot do here: hand the driver several buffers and let
// it work through them, so it never has to come back and wait for the next.
// If the 9-11 % that pyserial leaves on the line is the gap between one write
// finishing and the next being issued, this is what closes it.
static void test_sink_async(Link& L, int baud, int nbytes, int chunk, int depth) {
    std::string ack = command(L, "r");
    if (ack.rfind("OK sink", 0) != 0) { printf("  async  refused: %s\n", ack.c_str()); return; }

    std::vector<char> data(nbytes);
    for (int i = 0; i < nbytes; i++) data[i] = (char)(i & 0xFF);

    std::vector<OVERLAPPED> ov(depth);
    std::vector<HANDLE> ev(depth);
    std::vector<bool> busy(depth, false);
    for (int i = 0; i < depth; i++) {
        ZeroMemory(&ov[i], sizeof(OVERLAPPED));
        ev[i] = CreateEventA(nullptr, TRUE, FALSE, nullptr);
        ov[i].hEvent = ev[i];
    }

    COMMTIMEOUTS ct;
    ct.ReadIntervalTimeout = MAXDWORD;
    ct.ReadTotalTimeoutMultiplier = MAXDWORD;
    ct.ReadTotalTimeoutConstant = 50;
    ct.WriteTotalTimeoutMultiplier = 0;
    ct.WriteTotalTimeoutConstant = 5000;
    SetCommTimeouts(L.h, &ct);

    int slot = 0;
    double t0 = now_ms();
    for (int off = 0; off < nbytes; off += chunk) {
        if (busy[slot]) {
            DWORD done = 0;
            GetOverlappedResult(L.h, &ov[slot], &done, TRUE);
            busy[slot] = false;
        }
        ResetEvent(ev[slot]);
        DWORD done = 0;
        int n = std::min(chunk, nbytes - off);
        if (!WriteFile(L.h, data.data() + off, n, &done, &ov[slot])) {
            if (GetLastError() == ERROR_IO_PENDING) busy[slot] = true;
        }
        slot = (slot + 1) % depth;
    }
    for (int i = 0; i < depth; i++) {
        if (busy[i]) {
            DWORD done = 0;
            GetOverlappedResult(L.h, &ov[i], &done, TRUE);
        }
    }
    double t1 = now_ms();
    for (int i = 0; i < depth; i++) CloseHandle(ev[i]);

    std::string rep = line(L, 3.0);
    double ms = t1 - t0;
    double kbs = ms > 0 ? nbytes / ms : 0.0;
    printf("  async  %d B in %.1f ms = %.1f kB/s, %.0f%% of the line, "
           "%d B chunks, %d outstanding (device says %.0f ms)\n",
           nbytes, ms, kbs, kbs * 1000 * BITS_PER_BYTE / baud * 100, chunk, depth,
           field(rep, "us") / 1000.0);
    if (field(rep, "bad") || field(rep, "n") != nbytes)
        printf("         !! %ld wrong bytes, %ld never arrived\n",
               field(rep, "bad"), nbytes - field(rep, "n"));
}

// ----------------------------------------------------------------- round trip

static void test_echo(Link& L, int rounds) {
    std::string ack = command(L, "e");
    if (ack.rfind("OK echo", 0) != 0) { printf("  echo   refused: %s\n", ack.c_str()); return; }

    std::vector<double> times;
    times.reserve(rounds);
    for (int i = 0; i < rounds; i++) {
        char out = (char)(i & 0xFF), in = 0;
        double t0 = now_ms();
        write_all(L, &out, 1);
        DWORD got = 0;
        double deadline = t0 + 1000.0;
        while (!got && now_ms() < deadline) got = io(L, false, &in, 1, 1000);
        double t1 = now_ms();
        if (got) times.push_back(t1 - t0);
    }
    Sleep(400);
    clear(L);

    if (times.empty()) { printf("  echo   nothing came back\n"); return; }
    std::sort(times.begin(), times.end());
    printf("\n  echo   one byte out and back: min %.2f ms, median %.2f, p90 %.2f, "
           "max %.2f over %zu rounds\n",
           times.front(), times[times.size() / 2], times[(size_t)(times.size() * 0.9)],
           times.back(), times.size());
}

// ------------------------------------------------------------------- lockstep

static void test_lockstep(Link& L, int baud, int records, int window, int confirm) {
    std::string ack = command(L, "w " + std::to_string(confirm * RECORD));
    if (ack.rfind("OK win", 0) != 0) { printf("  lock   refused: %s\n", ack.c_str()); return; }

    char payload[RECORD];
    for (int i = 0; i < RECORD; i++) payload[i] = (char)i;

    int written = 0, acked = 0, waits = 0;
    bool stalled = false;
    std::vector<double> wait_ms;

    double t0 = now_ms();
    while (written < records) {
        while (written < records && (written - acked) < window) {
            write_all(L, payload, RECORD);
            written++;
        }
        if (written >= records) break;

        double w0 = now_ms();
        double deadline = w0 + 3000.0;
        int before = acked;
        while ((written - acked) >= window && now_ms() < deadline) {
            if (!pump(L)) continue;
            size_t nl;
            while ((nl = L.buf.find('\n')) != std::string::npos) {
                std::string text = L.buf.substr(0, nl);
                L.buf.erase(0, nl + 1);
                if (!text.empty() && text[0] == 'A')
                    acked = (int)(strtol(text.c_str() + 1, nullptr, 10) / RECORD);
            }
        }
        wait_ms.push_back(now_ms() - w0);
        waits++;
        if (acked == before) { stalled = true; break; }
    }

    double deadline = now_ms() + (stalled ? 0.0 : 3000.0);
    while (acked < (records / confirm) * confirm && now_ms() < deadline) {
        if (!pump(L)) continue;
        size_t nl;
        while ((nl = L.buf.find('\n')) != std::string::npos) {
            std::string text = L.buf.substr(0, nl);
            L.buf.erase(0, nl + 1);
            if (!text.empty() && text[0] == 'A')
                acked = (int)(strtol(text.c_str() + 1, nullptr, 10) / RECORD);
        }
    }
    double t1 = now_ms();
    Sleep(400);
    clear(L);

    if (stalled) {
        printf("  lock   window %2d, confirm every %2d: stalled after %d records, "
               "%d confirmed\n", window, confirm, written, acked);
        return;
    }
    double ms = t1 - t0;
    double per = ms / records;
    double kbs = ms > 0 ? (double)records * RECORD / ms : 0.0;
    std::sort(wait_ms.begin(), wait_ms.end());
    printf("  lock   window %2d, confirm every %2d: %.2f ms/record = %.1f kB/s, "
           "%.0f%% of the line (%d waits of %.2f ms; the record itself needs %.2f ms)\n",
           window, confirm, per, kbs, kbs * 1000 * BITS_PER_BYTE / baud * 100,
           waits, wait_ms.empty() ? 0.0 : wait_ms[wait_ms.size() / 2],
           wire_ms(RECORD, baud));
}

// ----------------------------------------------------------------------- main

int main(int argc, char** argv) {
    LARGE_INTEGER f;
    QueryPerformanceFrequency(&f);
    g_freq = (double)f.QuadPart;

    const char* port = argc > 1 ? argv[1] : "COM18";
    int baud = argc > 2 ? atoi(argv[2]) : 500000;
    int nbytes = argc > 3 ? atoi(argv[3]) : 32768;
    const int records = 512;

    Link L;
    if (!open_link(L, port, 500000)) return 1;
    Sleep(2000);   // the board is rebooting because the port was opened
    clear(L);

    std::string hello = command(L, "v");
    if (hello.rfind("SB", 0) != 0) {
        printf("not the serial bench firmware (said \"%s\") - flash it with "
               "`pio run -e serialbench -t upload`\n", hello.c_str());
        return 1;
    }

    if (baud != 500000) {
        std::string reply = command(L, "b " + std::to_string(baud));
        if (reply.rfind("OK baud", 0) != 0) {
            printf("baud not accepted: %s\n", reply.c_str());
            return 1;
        }
        DCB dcb;
        ZeroMemory(&dcb, sizeof(dcb));
        dcb.DCBlength = sizeof(dcb);
        GetCommState(L.h, &dcb);
        dcb.BaudRate = baud;
        SetCommState(L.h, &dcb);
        Sleep(100);
        clear(L);
    }

    printf("\n=== %s @ %d baud   %s   (C++, raw Win32)\n", port, baud, hello.c_str());
    printf("    a byte takes %.1f us, so the line is worth %.1f kB/s\n\n",
           1000000.0 * BITS_PER_BYTE / baud, baud / (double)BITS_PER_BYTE / 1000);

    test_source(L, baud, nbytes);

    int chunks[] = {RECORD, RECORD * WINDOW, 4096};
    for (int c : chunks) test_sink(L, baud, nbytes, c);
    test_sink_async(L, baud, nbytes, RECORD, 8);
    test_sink_async(L, baud, nbytes, 4096, 4);

    test_echo(L, 200);

    int windows[][2] = {{WINDOW, CONFIRM}, {WINDOW, 1}, {14, CONFIRM}, {28, 8}, {1, 1}};
    for (auto& w : windows) test_lockstep(L, baud, records, w[0], w[1]);

    if (baud != 500000) command(L, "b 500000");
    CloseHandle(L.ev);
    CloseHandle(L.h);
    return 0;
}
