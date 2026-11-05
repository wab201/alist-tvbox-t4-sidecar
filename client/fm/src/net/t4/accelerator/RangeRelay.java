package net.t4.accelerator;

import java.io.ByteArrayInputStream;
import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.HashMap;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** One continuous direct stream; repair only its missing tail, never cache a movie. */
public final class RangeRelay {
    private static final Pattern RANGE = Pattern.compile("bytes=(\\d*)-(\\d*)");
    private static final Pattern BOUNDS = Pattern.compile("bytes (\\d+)-(\\d+)/(\\d+)");
    private static final long METADATA_TTL = 30_000_000_000L;
    private static final long IDLE_REOPEN = 15_000_000_000L;
    private static final Map<MetadataKey, Metadata> metadata = new LinkedHashMap<>();

    private static final class MetadataKey {
        final String url;
        final Map<String, String> headers;

        MetadataKey(String url, Map<String, String> headers) {
            this.url = url;
            this.headers = new HashMap<>(headers);
        }

        @Override public boolean equals(Object other) {
            if (!(other instanceof MetadataKey)) return false;
            MetadataKey key = (MetadataKey) other;
            return url.equals(key.url) && headers.equals(key.headers);
        }

        @Override public int hashCode() { return 31 * url.hashCode() + headers.hashCode(); }
    }

    private static final class Metadata {
        final long total, created = System.nanoTime();
        final String etag, mime, modified;

        Metadata(long total, String etag, String mime, String modified) {
            this.total = total;
            this.etag = etag;
            this.mime = mime;
            this.modified = modified;
        }
    }

    private static synchronized Metadata cached(MetadataKey key) {
        Iterator<Metadata> entries = metadata.values().iterator();
        while (entries.hasNext()) {
            if (System.nanoTime() - entries.next().created >= METADATA_TTL) entries.remove();
        }
        return metadata.get(key);
    }

    private static synchronized void remember(MetadataKey key, Metadata value) {
        metadata.remove(key);
        metadata.put(key, value);
        while (metadata.size() > 8) metadata.remove(metadata.keySet().iterator().next());
    }

    private static synchronized void forget(MetadataKey key) { metadata.remove(key); }

    public static synchronized void clearMetadata() { metadata.clear(); }

    public static final class Reply {
        public final int status;
        public final String mime;
        public final InputStream body;
        public final Map<String, String> headers;
        public final long length;

        Reply(int status, String mime, InputStream body, Map<String, String> headers, long length) {
            this.status = status;
            this.mime = mime;
            this.body = body;
            this.headers = headers;
            this.length = length;
        }
    }

    public static HttpURLConnection connect(String url, String method, Map<String, String> headers)
            throws IOException {
        URL target = new URL(url);
        if (!target.getProtocol().equals("http") && !target.getProtocol().equals("https")) {
            throw new IOException("Unsupported media protocol");
        }
        HttpURLConnection connection = (HttpURLConnection) target.openConnection();
        connection.setConnectTimeout(4000);
        connection.setReadTimeout(5000);
        connection.setInstanceFollowRedirects(true);
        connection.setRequestMethod(method);
        for (Map.Entry<String, String> entry : headers.entrySet()) {
            connection.setRequestProperty(entry.getKey(), entry.getValue());
        }
        connection.setRequestProperty("Accept-Encoding", "identity");
        return connection;
    }

    private static String header(HttpURLConnection connection, String name) {
        String value = connection.getHeaderField(name);
        return value == null ? "" : value;
    }

    private static long size(HttpURLConnection connection) {
        try {
            return Long.parseLong(header(connection, "Content-Length"));
        } catch (NumberFormatException error) {
            return -1;
        }
    }

    private static boolean strong(String value) {
        return value.length() >= 2 && value.startsWith("\"") && value.endsWith("\"");
    }

    private static Map<String, String> responseHeaders(HttpURLConnection connection) {
        Map<String, String> result = new HashMap<>();
        for (String name : new String[]{
                "Content-Range", "Accept-Ranges", "ETag", "Last-Modified", "Content-Encoding"}) {
            String value = header(connection, name);
            if (!value.isEmpty()) result.put(name, value);
        }
        // Transfer framing is built separately from Reply.length.
        return result;
    }

    public static Reply open(String direct, String relay, Map<String, String> headers,
                             String range, String ifRange) throws IOException {
        HttpURLConnection head = null;
        MetadataKey key = new MetadataKey(direct, headers);
        Metadata known = cached(key);
        long total = -1;
        String etag = "", mime = "application/octet-stream", modified = "";
        if (known != null) {
            total = known.total;
            etag = known.etag;
            mime = known.mime;
            modified = known.modified;
        } else try {
            head = connect(direct, "HEAD", headers);
            // Optional metadata must not consume the full media timeout before a healthy GET.
            head.setConnectTimeout(1500);
            head.setReadTimeout(1000);
            String encoding = header(head, "Content-Encoding");
            if (head.getResponseCode() == 200 && (encoding.isEmpty() || encoding.equalsIgnoreCase("identity"))) {
                total = size(head);
                etag = header(head, "ETag");
                modified = header(head, "Last-Modified");
                if (!header(head, "Content-Type").isEmpty()) mime = header(head, "Content-Type");
                if (total > 0 && strong(etag)
                        && !header(head, "Cache-Control").toLowerCase(java.util.Locale.ROOT).contains("no-store")) {
                    remember(key, new Metadata(total, etag, mime, modified));
                }
            }
        } catch (IOException ignored) {
            // Initial connection failure can select relay before any bytes are emitted.
        } finally {
            if (head != null) head.disconnect();
        }
        if (total <= 0 || !strong(etag)) {
            return single(direct, relay, headers, range, ifRange);
        }
        if (!ifRange.isEmpty() && !ifRange.equals(etag) && !ifRange.equals(modified)) range = "";
        long start = 0, end = total - 1;
        int status = 200;
        Map<String, String> output = new HashMap<>();
        output.put("Accept-Ranges", "bytes");
        output.put("ETag", etag);
        if (!modified.isEmpty()) output.put("Last-Modified", modified);
        if (!range.isEmpty()) {
            Matcher match = RANGE.matcher(range);
            try {
                if (!match.matches() || match.group(1).isEmpty() && match.group(2).isEmpty()) {
                    throw new NumberFormatException();
                }
                if (match.group(1).isEmpty()) {
                    long suffix = Long.parseLong(match.group(2));
                    if (suffix <= 0) throw new NumberFormatException();
                    start = Math.max(0, total - suffix);
                } else {
                    start = Long.parseLong(match.group(1));
                    if (!match.group(2).isEmpty()) end = Math.min(end, Long.parseLong(match.group(2)));
                }
                if (start >= total || start > end) throw new NumberFormatException();
            } catch (NumberFormatException error) {
                output.put("Content-Range", "bytes */" + total);
                return new Reply(416, mime, new ByteArrayInputStream(new byte[0]), output, 0);
            }
            status = 206;
            output.put("Content-Range", "bytes " + start + "-" + end + "/" + total);
        }
        return new Reply(status, mime,
                new RepairStream(direct, relay, headers, etag, total, start, end, key),
                output, end - start + 1);
    }

    private static Reply single(String direct, String relay, Map<String, String> headers,
                                String range, String ifRange) throws IOException {
        IOException failure = null;
        for (String target : new String[]{direct, relay}) {
            HttpURLConnection connection = null;
            try {
                Map<String, String> request = target.equals(direct)
                        ? new HashMap<>(headers) : new HashMap<String, String>();
                if (!range.isEmpty()) request.put("Range", range);
                if (!ifRange.isEmpty()) request.put("If-Range", ifRange);
                connection = connect(target, "GET", request);
                int status = connection.getResponseCode();
                if (status != 200 && status != 206 && status != 416) {
                    throw new IOException("Media upstream rejected request");
                }
                final HttpURLConnection owned = connection;
                InputStream body = status == 416 ? new ByteArrayInputStream(new byte[0])
                        : connection.getInputStream();
                String mime = header(connection, "Content-Type");
                final long expected = status == 416 ? 0 : size(connection);
                return new Reply(status, mime.isEmpty() ? "application/octet-stream" : mime,
                        new java.io.FilterInputStream(body) {
                            private long received;
                            private int checked(int count) throws IOException {
                                if (count < 0 && expected >= 0 && received < expected) {
                                    throw new EOFException("Media upstream truncated response");
                                }
                                if (count > 0) received += count;
                                return count;
                            }
                            @Override public int read(byte[] buffer, int offset, int length)
                                    throws IOException {
                                return checked(in.read(buffer, offset, length));
                            }
                            @Override public int read() throws IOException {
                                int value = in.read();
                                checked(value < 0 ? -1 : 1);
                                return value;
                            }
                            @Override public void close() throws IOException {
                                try { super.close(); } finally { owned.disconnect(); }
                            }
                        }, responseHeaders(connection), expected);
            } catch (IOException error) {
                if (connection != null) connection.disconnect();
                failure = error;
            }
        }
        throw failure;
    }

    private static final class RepairStream extends InputStream {
        private final String direct, relay, etag;
        private final Map<String, String> headers;
        private final long total, end;
        private long position;
        private boolean repaired, directRetried;
        private long lastRead;
        private final MetadataKey key;
        private volatile boolean closed;
        private volatile HttpURLConnection connection;
        private InputStream stream;
        private final byte[] singleByte = new byte[1];

        RepairStream(String direct, String relay, Map<String, String> headers,
                     String etag, long total, long start, long end, MetadataKey key) {
            this.direct = direct;
            this.relay = relay;
            this.headers = new HashMap<>(headers);
            this.etag = etag;
            this.total = total;
            this.position = start;
            this.end = end;
            this.key = key;
        }

        private void start(boolean viaRelay) throws IOException {
            if (closed) throw new IOException("Media request closed");
            Map<String, String> request = viaRelay ? new HashMap<String, String>()
                    : new HashMap<>(headers);
            request.put("Range", "bytes=" + position + "-" + end);
            request.put("If-Range", etag);
            HttpURLConnection next = connect(viaRelay ? relay : direct, "GET", request);
            if (!viaRelay && directRetried) {
                next.setConnectTimeout(1500);
                next.setReadTimeout(1500);
            }
            connection = next;
            try {
                if (closed) throw new IOException("Media request closed");
                int status = next.getResponseCode();
                if (status >= 500) throw new IOException("Media upstream temporarily unavailable");
                Matcher bounds = BOUNDS.matcher(header(next, "Content-Range"));
                String encoding = header(next, "Content-Encoding");
                boolean partial = status == 206 && bounds.matches()
                        && Long.parseLong(bounds.group(1)) == position
                        && Long.parseLong(bounds.group(2)) == end
                        && Long.parseLong(bounds.group(3)) == total;
                boolean whole = !viaRelay && status == 200 && position == 0
                        && end == total - 1 && size(next) == total;
                if ((!partial && !whole)
                        || !etag.equals(header(next, "ETag"))
                        || !encoding.isEmpty() && !encoding.equalsIgnoreCase("identity")) {
                    throw new InvalidMediaException();
                }
                stream = next.getInputStream();
                if (closed) throw new IOException("Media request closed");
            } catch (IOException error) {
                next.disconnect();
                throw error;
            } catch (RuntimeException error) {
                next.disconnect();
                throw new IOException("Media range unavailable", error);
            }
        }

        private void disconnect() {
            HttpURLConnection active = connection;
            if (active != null) active.disconnect();
            connection = null;
            stream = null;
        }

        @Override public int read(byte[] buffer, int offset, int length) throws IOException {
            if (closed) throw new IOException("Media request closed");
            if (buffer == null) throw new NullPointerException();
            if (offset < 0 || length < 0 || offset > buffer.length - length) {
                throw new IndexOutOfBoundsException();
            }
            if (length == 0) return 0;
            if (position > end) { disconnect(); return -1; }
            // No pause callback is required: reconnect only when the consumer reads again.
            if (stream != null && System.nanoTime() - lastRead >= IDLE_REOPEN) disconnect();
            for (;;) {
                try {
                    if (stream == null) start(repaired);
                    InputStream active = stream;
                    if (closed || active == null) throw new IOException("Media request closed");
                    int count = active.read(buffer, offset, (int) Math.min(length, end - position + 1));
                    if (closed) throw new IOException("Media request closed");
                    if (count < 0) throw new EOFException("Media upstream truncated response");
                    position += count;
                    lastRead = System.nanoTime();
                    return count;
                } catch (IOException error) {
                    disconnect();
                    forget(key);
                    if (closed || repaired) throw error;
                    if (!directRetried && !(error instanceof InvalidMediaException)) {
                        directRetried = true;
                        continue;
                    }
                    repaired = true;
                }
            }
        }

        @Override public int read() throws IOException {
            return read(singleByte, 0, 1) < 0 ? -1 : singleByte[0] & 255;
        }

        @Override public void close() {
            closed = true;
            disconnect();
        }
    }

    private static final class InvalidMediaException extends IOException {
        InvalidMediaException() { super("Media object or byte range changed"); }
    }
}
