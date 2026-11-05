import java.io.InputStream;
import java.security.MessageDigest;
import java.util.HashMap;
import java.util.Map;

import net.t4.accelerator.RangeRelay;

public final class RangeRelayHarness {
    public static void main(String[] args) throws Exception {
        Map<String, String> headers = new HashMap<>();
        headers.put("User-Agent", "signed-agent");
        headers.put("Referer", "https://origin.example/");
        RangeRelay.Reply response = RangeRelay.open(args[0], args[1], headers, args[2], args[3]);
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        long count = 0;
        boolean failed = false;
        try (InputStream input = response.body) {
            if (args.length > 4 && args[4].equals("head")) {
                // FM handles HEAD by closing the returned stream without reading it.
            } else if (args.length > 4 && args[4].equals("single-byte")) {
                for (int value; (value = input.read()) >= 0;) {
                    digest.update((byte) value);
                    count++;
                }
            } else {
                byte[] buffer = new byte[32768];
                boolean paused = false;
                for (int size; (size = input.read(buffer)) >= 0;) {
                    digest.update(buffer, 0, size);
                    count += size;
                    if (args.length > 4 && args[4].equals("cancel")) break;
                    if (!paused && args.length > 4 && args[4].equals("pause")) {
                        paused = true;
                        Thread.sleep(16000);
                    }
                }
            }
        } catch (java.io.IOException error) {
            failed = true;
        }
        StringBuilder hash = new StringBuilder();
        for (byte value : digest.digest()) hash.append(String.format("%02x", value & 255));
        System.out.println(response.status);
        System.out.println(count);
        System.out.println(hash);
        System.out.println(failed);
        System.out.println(response.headers.getOrDefault("Content-Range", ""));
        if (args.length > 4 && args[4].equals("cache-expiry")) {
            Thread.sleep(31000);
            try (InputStream input = RangeRelay.open(
                    args[0], args[1], headers, "bytes=100000-108191", "").body) {
                byte[] buffer = new byte[32768];
                while (input.read(buffer) >= 0) {}
            }
        }
        if (args.length > 4 && args[4].equals("cache-change")) {
            boolean rejected = false;
            try (InputStream input = RangeRelay.open(
                    args[0], args[1], headers, "bytes=100000-108191", "").body) {
                input.read(new byte[8192]);
            } catch (java.io.IOException expected) {
                rejected = true;
            }
            if (!rejected) throw new IllegalStateException("Mixed media objects");
            try (InputStream input = RangeRelay.open(
                    args[0], args[1], headers, "bytes=100000-108191", "").body) {
                long size = 0;
                byte[] buffer = new byte[32768];
                for (int value; (value = input.read(buffer)) >= 0;) size += value;
                if (size != 8192) throw new IllegalStateException("Fresh metadata not loaded");
            }
        }
        if (args.length > 4 && args[4].equals("cancel-blocked")) {
            RangeRelay.Reply pending = RangeRelay.open(args[0], args[1], headers, "", "");
            InputStream input = pending.body;
            input.read(new byte[8192]);
            Thread reader = new Thread(() -> {
                try { input.read(new byte[8192]); } catch (java.io.IOException expected) {}
            });
            reader.start();
            Thread.sleep(200);
            input.close();
            reader.join(6500);
            if (reader.isAlive()) throw new IllegalStateException("Media reader leaked");
        }
        if (args.length > 4 && args[4].startsWith("seek")) {
            for (String range : new String[]{"bytes=100000-108191", "bytes=0-8191", "bytes=-8192"}) {
                if (args[4].equals("seek-headers")) headers.put("User-Agent", range);
                RangeRelay.Reply seek = RangeRelay.open(args[0], args[1], headers, range, "");
                try (InputStream input = seek.body) {
                    byte[] buffer = new byte[32768];
                    MessageDigest part = MessageDigest.getInstance("SHA-256");
                    long bytes = 0;
                    for (int size; (size = input.read(buffer)) >= 0;) {
                        part.update(buffer, 0, size);
                        bytes += size;
                    }
                    StringBuilder hashPart = new StringBuilder();
                    for (byte value : part.digest()) hashPart.append(String.format("%02x", value & 255));
                    System.out.println(bytes + ":" + hashPart);
                }
            }
        }
    }
}
