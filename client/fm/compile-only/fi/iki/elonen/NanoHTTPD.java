package fi.iki.elonen;

import java.io.InputStream;

/** Compile-only ABI. The HTTP implementation is supplied by FM, not this JAR. */
public class NanoHTTPD {
    public static class Response {
        public interface IStatus {}
        public enum Status implements IStatus {
            OK;
            public static Status lookup(int code) { return OK; }
        }
        public void addHeader(String name, String value) {}
    }
    public static Response newFixedLengthResponse(
            Response.IStatus status, String mime, InputStream stream, long length) {
        return null;
    }
    public static Response newChunkedResponse(
            Response.IStatus status, String mime, InputStream stream) {
        return null;
    }
}
