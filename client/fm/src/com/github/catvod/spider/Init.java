package com.github.catvod.spider;

import android.content.Context;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.Collections;
import java.util.Properties;

import net.t4.accelerator.RangeRelay;

import org.json.JSONObject;

public final class Init {
    private static volatile Properties settings;

    public static void init(Context context) {
        // A failed optional helper must never abort FM subscription initialization.
        try {
            Class.forName("com.github.catvod.Proxy").getMethod("getUrl", boolean.class);
            Properties loaded = new Properties();
            try (InputStream input = Init.class.getClassLoader()
                    .getResourceAsStream("t4-accelerator.properties")) {
                if (input == null) return;
                loaded.load(input);
            }
            settings = loaded;
            RangeRelay.clearMetadata();
            Thread thread = new Thread(() -> probe(loaded), "t4-capability-probe");
            thread.setDaemon(true);
            thread.start();
        } catch (Throwable ignored) {
            settings = null;
        }
    }

    static boolean owns(String session) {
        Properties current = settings;
        return current != null && current.getProperty("session", "").equals(session);
    }

    static boolean accepts(String plan) {
        Properties current = settings;
        return current != null && plan.startsWith(current.getProperty("base")
                + "/t4/acceleration/" + current.getProperty("session") + "/plans/");
    }

    static String challenge() {
        Properties current = settings;
        return current == null ? "" : current.getProperty("challenge", "");
    }

    static byte[] read(InputStream input, int limit) throws Exception {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        byte[] buffer = new byte[4096];
        for (int count; (count = input.read(buffer)) >= 0;) {
            if (output.size() + count > limit) throw new IllegalStateException("Response too large");
            output.write(buffer, 0, count);
        }
        return output.toByteArray();
    }

    private static void probe(Properties loaded) {
        String session = loaded.getProperty("session");
        String challenge = loaded.getProperty("challenge");
        // Init precedes registration of Proxy.proxy; allow a short bounded registration window.
        for (int attempt = 0; attempt < 8; attempt++) {
            HttpURLConnection local = null, partial = null, head = null, ready = null;
            try {
                Thread.sleep(250);
                String endpoint = (String) Class.forName("com.github.catvod.Proxy")
                        .getMethod("getUrl", boolean.class).invoke(null, true);
                if (endpoint == null || !endpoint.startsWith("http://127.0.0.1:")) return;
                String query = "?do=t4probe&session=" + URLEncoder.encode(session, "UTF-8")
                        + "&siteKey=" + URLEncoder.encode(loaded.getProperty("siteKey"), "UTF-8");
                local = RangeRelay.connect(endpoint + query, "GET", Collections.emptyMap());
                local.setConnectTimeout(500);
                local.setReadTimeout(500);
                if (local.getResponseCode() != 200) continue;
                String response;
                try (InputStream input = local.getInputStream()) {
                    response = new String(read(input, 256), StandardCharsets.UTF_8);
                }
                if (!challenge.equals(response)) continue;
                if (!Integer.toString(challenge.length()).equals(local.getHeaderField("Content-Length"))) continue;
                partial = RangeRelay.connect(endpoint + query, "GET",
                        Collections.singletonMap("Range", "bytes=0-7"));
                partial.setConnectTimeout(500);
                partial.setReadTimeout(500);
                if (partial.getResponseCode() != 206
                        || !("bytes 0-7/" + challenge.length()).equals(partial.getHeaderField("Content-Range"))
                        || !"8".equals(partial.getHeaderField("Content-Length"))) continue;
                try (InputStream input = partial.getInputStream()) {
                    if (!challenge.substring(0, 8).equals(
                            new String(read(input, 256), StandardCharsets.UTF_8))) continue;
                }
                head = RangeRelay.connect(endpoint + query, "HEAD", Collections.emptyMap());
                head.setConnectTimeout(500);
                head.setReadTimeout(500);
                if (head.getResponseCode() != 200 || !Integer.toString(challenge.length())
                        .equals(head.getHeaderField("Content-Length"))) continue;
                ready = RangeRelay.connect(loaded.getProperty("base")
                        + "/t4/acceleration/" + session + "/ready", "POST", Collections.emptyMap());
                ready.setDoOutput(true);
                ready.setRequestProperty("Content-Type", "application/json");
                byte[] body = new JSONObject().put("proof", challenge).put("protocol", 1)
                        .put("version", loaded.getProperty("version")).toString()
                        .getBytes(StandardCharsets.UTF_8);
                ready.setFixedLengthStreamingMode(body.length);
                try (OutputStream output = ready.getOutputStream()) { output.write(body); }
                if (ready.getResponseCode() == 200) return;
            } catch (Exception ignored) {
                // No successful loopback round trip means pure T4 remains selected.
            } finally {
                if (local != null) local.disconnect();
                if (partial != null) partial.disconnect();
                if (head != null) head.disconnect();
                if (ready != null) ready.disconnect();
            }
        }
    }
}
