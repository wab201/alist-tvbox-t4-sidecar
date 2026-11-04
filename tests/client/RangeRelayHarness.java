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
            } else {
                byte[] buffer = new byte[32768];
                for (int size; (size = input.read(buffer)) >= 0;) {
                    digest.update(buffer, 0, size);
                    count += size;
                    if (args.length > 4 && args[4].equals("cancel")) break;
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
    }
}
