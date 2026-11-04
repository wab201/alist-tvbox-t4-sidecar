package fi.iki.elonen;

import java.io.InputStream;
import java.util.HashMap;
import java.util.Map;

/** Response-ABI fixture only; the real FM server remains a separate device gate. */
public class NanoHTTPD {
    public static class Response {
        public interface IStatus { int getRequestStatus(); }
        public enum Status implements IStatus {
            OK(200), PARTIAL_CONTENT(206), RANGE_NOT_SATISFIABLE(416);
            private final int code;
            Status(int code) { this.code = code; }
            public int getRequestStatus() { return code; }
            public static Status lookup(int code) {
                for (Status status : values()) if (status.code == code) return status;
                throw new IllegalArgumentException();
            }
        }
        public final IStatus status;
        public final InputStream body;
        public final long length;
        public final Map<String, String> headers = new HashMap<>();
        public Response(IStatus status, InputStream body, long length) {
            this.status = status;
            this.body = body;
            this.length = length;
        }
        public void addHeader(String name, String value) { headers.put(name, value); }
    }
    public static Response newFixedLengthResponse(
            Response.IStatus status, String mime, InputStream stream, long length) {
        return new Response(status, stream, length);
    }
    public static Response newChunkedResponse(
            Response.IStatus status, String mime, InputStream stream) {
        return new Response(status, stream, -1);
    }
}
