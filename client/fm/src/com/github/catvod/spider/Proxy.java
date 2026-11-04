package com.github.catvod.spider;

import java.io.ByteArrayInputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.nio.charset.StandardCharsets;
import java.util.Collections;
import java.util.Arrays;
import java.util.HashMap;
import java.util.Iterator;
import java.util.Map;

import net.t4.accelerator.RangeRelay;

import org.json.JSONObject;
import fi.iki.elonen.NanoHTTPD;

public final class Proxy {
    public static Object[] proxy(Map<String, String> params) throws Exception {
        if (!Init.owns(params.get("session"))) return null;
        if ("t4probe".equals(params.get("do"))) {
            byte[] body = Init.challenge().getBytes(StandardCharsets.UTF_8);
            Map<String, String> headers = new HashMap<>();
            headers.put("Accept-Ranges", "bytes");
            if ("bytes=0-7".equals(params.get("range"))) {
                headers.put("Content-Range", "bytes 0-7/" + body.length);
                return response(206, "text/plain", new ByteArrayInputStream(Arrays.copyOf(body, 8)),
                        headers, 8);
            }
            return response(200, "text/plain", new ByteArrayInputStream(body), headers, body.length);
        }
        if (!"t4accel".equals(params.get("do"))) return null;
        String plan = params.get("plan");
        if (plan == null || !Init.accepts(plan)) return null;
        JSONObject object;
        HttpURLConnection connection = RangeRelay.connect(plan, "GET", Collections.emptyMap());
        try {
            if (connection.getResponseCode() != 200) throw new IllegalStateException("Plan expired");
            try (InputStream input = connection.getInputStream()) {
                object = new JSONObject(new String(Init.read(input, 65536), StandardCharsets.UTF_8));
            }
        } finally {
            connection.disconnect();
        }
        if (object.getInt("protocol") != 1) throw new IllegalStateException("Protocol mismatch");
        JSONObject values = object.getJSONObject("headers");
        Map<String, String> headers = new HashMap<>();
        for (Iterator<String> keys = values.keys(); keys.hasNext();) {
            String key = keys.next();
            if (!key.equalsIgnoreCase("Range") && !key.equalsIgnoreCase("If-Range")
                    && !key.equalsIgnoreCase("Accept-Encoding")) headers.put(key, values.getString(key));
        }
        RangeRelay.Reply reply = RangeRelay.open(object.getString("direct"),
                object.getString("relay"), headers,
                params.containsKey("range") ? params.get("range") : "",
                params.containsKey("if-range") ? params.get("if-range") : "");
        return response(reply.status, reply.mime, reply.body, reply.headers, reply.length);
    }

    private static Object[] response(int status, String mime, InputStream stream,
                                     Map<String, String> headers, long length) throws Exception {
        try {
            NanoHTTPD.Response result = length >= 0
                    ? NanoHTTPD.newFixedLengthResponse(NanoHTTPD.Response.Status.lookup(status), mime, stream, length)
                    : NanoHTTPD.newChunkedResponse(NanoHTTPD.Response.Status.lookup(status), mime, stream);
            for (Map.Entry<String, String> entry : headers.entrySet()) {
                result.addHeader(entry.getKey(), entry.getValue());
            }
            return new Object[]{result};
        } catch (RuntimeException | Error error) {
            try { stream.close(); } catch (Exception ignored) {}
            throw error;
        }
    }
}
