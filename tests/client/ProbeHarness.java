import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URLDecoder;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;

import com.sun.net.httpserver.HttpServer;
import com.github.catvod.spider.Init;
import com.github.catvod.spider.T4Relay;
import fi.iki.elonen.NanoHTTPD;

public final class ProbeHarness {
    public static void main(String[] args) throws Exception {
        final String mode = args[0];
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/proxy", exchange -> {
            try {
                Map<String, String> params = new HashMap<>();
                for (String item : exchange.getRequestURI().getRawQuery().split("&")) {
                    String[] pair = item.split("=", 2);
                    params.put(URLDecoder.decode(pair[0], "UTF-8"),
                            URLDecoder.decode(pair[1], "UTF-8"));
                }
                exchange.getRequestHeaders().forEach((key, value) ->
                        params.put(key.toLowerCase(Locale.ROOT), value.get(0)));
                boolean head = exchange.getRequestMethod().equals("HEAD");
                if (mode.equals("no-fixed") || mode.equals("no-head") && head) {
                    exchange.sendResponseHeaders(500, -1);
                    return;
                }
                Object[] result = new T4Relay().proxy(params);
                NanoHTTPD.Response response = (NanoHTTPD.Response) result[0];
                response.headers.forEach((key, value) -> exchange.getResponseHeaders().set(key, value));
                int status = response.status.getRequestStatus();
                if (mode.equals("no-range") && status == 206) {
                    status = 200;
                    exchange.getResponseHeaders().remove("Content-Range");
                }
                if (response.length >= 0) {
                    exchange.getResponseHeaders().set("Content-Length", Long.toString(response.length));
                }
                exchange.sendResponseHeaders(status, head ? -1 : response.length);
                try (InputStream input = response.body; OutputStream output = exchange.getResponseBody()) {
                    if (!head) {
                        byte[] buffer = new byte[1024];
                        for (int size; (size = input.read(buffer)) >= 0;) {
                            if (mode.equals("wrong-body") && size > 0) buffer[0] ^= 1;
                            output.write(buffer, 0, size);
                        }
                    }
                }
            } catch (Exception error) {
                try { exchange.sendResponseHeaders(500, -1); } catch (Exception ignored) {}
            } finally {
                exchange.close();
            }
        });
        server.start();
        com.github.catvod.Proxy.endpoint = "http://127.0.0.1:" + server.getAddress().getPort() + "/proxy";
        try {
            Init.init(new android.content.Context());
            Thread.sleep(3500);
        } finally {
            server.stop(0);
        }
    }
}
