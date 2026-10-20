package com.tvmon.android;

import org.json.JSONArray;
import org.json.JSONObject;
import java.io.IOException;
import java.math.BigInteger;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Base64;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import javax.crypto.Cipher;
import javax.crypto.spec.IvParameterSpec;
import javax.crypto.spec.SecretKeySpec;

/** The desktop application's MPEG-TS VOD and ordinary AES-128 rules. */
final class VideoFormat {
    record Segment(String url, String keyUrl, byte[] iv, double duration) {}

    static Map<String, String> attributes(String line) {
        Map<String, String> result = new HashMap<>();
        Matcher m = Pattern.compile("([A-Z0-9-]+)=(?:\"([^\"]*)\"|([^,]*))").matcher(line);
        while (m.find()) result.put(m.group(1), m.group(2) != null ? m.group(2) : m.group(3));
        return result;
    }

    static List<Segment> playlist(byte[] body, String base) throws IOException {
        try {
            String text = new String(body, StandardCharsets.UTF_8).replaceFirst("^\\uFEFF", "");
            if (!text.trim().startsWith("#EXTM3U") || !text.contains("#EXT-X-ENDLIST"))
                throw new IOException("완료된 MPEG-TS 다시보기 영상만 저장할 수 있습니다.");
            List<Segment> result = new ArrayList<>();
            String key = null;
            byte[] explicitIv = null;
            BigInteger sequence = BigInteger.ZERO;
            Double duration = null;
            for (String raw : text.split("\\r?\\n")) {
                String line = raw.trim();
                if (line.isEmpty()) continue;
                if (line.startsWith("#EXT-X-MAP:") || line.startsWith("#EXT-X-BYTERANGE:")
                        || line.startsWith("#EXT-X-STREAM-INF:") || line.startsWith("#EXT-X-GAP"))
                    throw new IOException("이 영상의 재생 목록 형식은 지원하지 않습니다.");
                if (line.startsWith("#EXT-X-MEDIA-SEQUENCE:")) {
                    sequence = new BigInteger(line.substring(line.indexOf(':') + 1).trim());
                    iv(sequence);
                } else if (line.startsWith("#EXT-X-KEY:")) {
                    Map<String, String> fields = attributes(line);
                    if ("NONE".equals(fields.get("METHOD"))) { key = null; explicitIv = null; }
                    else if ("AES-128".equals(fields.get("METHOD")) && "identity".equals(fields.getOrDefault("KEYFORMAT", "identity"))) {
                        if (!fields.containsKey("URI")) throw new IllegalArgumentException();
                        key = SiteClient.resolve(base, fields.get("URI"));
                        String value = fields.get("IV");
                        explicitIv = value == null ? null : iv(new BigInteger(value.replaceFirst("^0[xX]", ""), 16));
                    } else throw new IOException("이 영상의 암호화 형식은 지원하지 않습니다.");
                } else if (line.startsWith("#EXTINF:")) {
                    duration = Double.parseDouble(line.substring(8).split(",", 2)[0]);
                    if (!Double.isFinite(duration) || duration <= 0) throw new IllegalArgumentException();
                } else if (!line.startsWith("#")) {
                    if (duration == null) throw new IllegalArgumentException();
                    result.add(new Segment(SiteClient.resolve(base, line), key, explicitIv != null ? explicitIv : iv(sequence), duration));
                    sequence = sequence.add(BigInteger.ONE);
                    duration = null;
                }
            }
            if (result.isEmpty() || duration != null) throw new IllegalArgumentException();
            return result;
        } catch (IOException e) { throw e; }
        catch (Exception e) { throw new IOException("영상 목록의 조각 또는 키 정보가 올바르지 않습니다.", e); }
    }

    static byte[] iv(BigInteger value) {
        if (value.signum() < 0 || value.bitLength() > 128) throw new IllegalArgumentException("IV");
        byte[] bytes = value.toByteArray(), result = new byte[16];
        int length = Math.min(16, bytes.length);
        System.arraycopy(bytes, bytes.length - length, result, 16 - length, length);
        return result;
    }

    private static byte[] base64(String value) { return Base64.getUrlDecoder().decode(value.replace('+', '-').replace('/', '_')); }
    private static int[] permutation(JSONArray array, int size) throws Exception {
        if (array.length() != size) throw new IllegalArgumentException();
        int[] result = new int[size]; boolean[] seen = new boolean[size];
        for (int i = 0; i < size; i++) {
            Object value = array.get(i);
            if (!(value instanceof Integer)) throw new IllegalArgumentException();
            int n = (Integer) value;
            if (n < 0 || n >= size || seen[n]) throw new IllegalArgumentException();
            seen[n] = true; result[i] = n;
        }
        return result;
    }

    static byte[] decodeKey(byte[] payload) throws IOException {
        if (payload.length == 16) return payload.clone();
        try {
            JSONObject response = new JSONObject(new String(payload, StandardCharsets.UTF_8));
            Map<String, JSONObject> layers = new HashMap<>();
            JSONArray array = response.getJSONArray("layers");
            for (int i = 0; i < array.length(); i++) { JSONObject layer = array.getJSONObject(i); layers.put(layer.getString("name"), layer); }
            byte[] data = base64(response.getString("encrypted_key"));
            byte[] mask = base64(layers.get("final_encrypt").getString("xor_mask"));
            JSONArray lengths = layers.get("decoy_shuffle").getJSONArray("segment_lengths");
            JSONArray positions = layers.get("decoy_shuffle").getJSONArray("real_positions");
            byte[] previous = base64(layers.get("xor_chain").getString("init_key"));
            int[] noise = permutation(layers.get("segment_noise").getJSONArray("perm"), 16);
            JSONArray rotations = layers.get("bit_rotate").getJSONArray("rotations");
            byte[] inverse = base64(layers.get("sbox").getString("inverse_sbox"));
            int[] bitsOrder = permutation(layers.get("bit_interleave").getJSONArray("perm"), 128);
            if (mask.length == 0 || previous.length == 0 || lengths.length() == 0 || positions.length() != 16
                    || rotations.length() == 0 || inverse.length != 256) throw new IllegalArgumentException();
            boolean[] sboxSeen = new boolean[256];
            for (byte b : inverse) { if (sboxSeen[b & 255]) throw new IllegalArgumentException(); sboxSeen[b & 255] = true; }
            int[] offsets = new int[lengths.length()]; int size = 0;
            for (int i = 0; i < lengths.length(); i++) {
                if (!(lengths.get(i) instanceof Integer) || lengths.getInt(i) <= 0 || lengths.getInt(i) > data.length - size) throw new IllegalArgumentException();
                offsets[i] = size; size += lengths.getInt(i);
            }
            for (int i = 0; i < size; i++) data[i] ^= mask[i % mask.length];
            byte[] layer3 = new byte[16]; boolean[] positionSeen = new boolean[lengths.length()];
            for (int i = 0; i < 16; i++) {
                if (!(positions.get(i) instanceof Integer)) throw new IllegalArgumentException();
                int p = positions.getInt(i);
                if (p < 0 || p >= lengths.length() || positionSeen[p]) throw new IllegalArgumentException();
                positionSeen[p] = true;
                layer3[noise[i]] = (byte) (data[offsets[p]] ^ previous[0]);
                previous = Arrays.copyOfRange(data, offsets[p], offsets[p] + Math.min(4, lengths.getInt(p)));
            }
            int[] bits = new int[128];
            for (int i = 0; i < 16; i++) {
                Object rotationValue = rotations.get(i % rotations.length());
                if (!(rotationValue instanceof Integer) || (Integer) rotationValue < 0) throw new IllegalArgumentException();
                int r = (Integer) rotationValue % 8, value = layer3[i] & 255;
                value = inverse[((value >>> r) | (value << (8 - r))) & 255] & 255;
                for (int bit = 0; bit < 8; bit++) bits[bitsOrder[i * 8 + bit]] = (value >>> (7 - bit)) & 1;
            }
            byte[] result = new byte[16];
            for (int i = 0; i < 128; i++) result[i / 8] |= (byte) (bits[i] << (7 - i % 8));
            return result;
        } catch (Exception e) { throw new IOException("사이트의 영상 키 응답을 읽지 못했습니다.", e); }
    }

    static byte[] decrypt(byte[] data, byte[] key, byte[] iv) throws IOException {
        try {
            if (key.length != 16 || iv.length != 16 || data.length == 0 || data.length % 16 != 0) throw new IllegalArgumentException();
            Cipher cipher = Cipher.getInstance("AES/CBC/PKCS5Padding");
            cipher.init(Cipher.DECRYPT_MODE, new SecretKeySpec(key, "AES"), new IvParameterSpec(iv));
            return cipher.doFinal(data);
        } catch (Exception e) { throw new IOException("영상 조각을 복호화하지 못했습니다.", e); }
    }

    static void validateTs(byte[] data) throws IOException {
        if (data.length == 0 || data.length % 188 != 0) throw new IOException("영상 조각이 손상되었습니다.");
        for (int i = 0; i < data.length; i += 188) if (data[i] != 0x47) throw new IOException("서버가 영상 대신 다른 응답을 반환했습니다.");
    }

    static String sha256(byte[] data) {
        try {
            StringBuilder result = new StringBuilder();
            for (byte b : MessageDigest.getInstance("SHA-256").digest(data)) result.append(String.format(java.util.Locale.ROOT, "%02x", b & 255));
            return result.toString();
        } catch (Exception e) { throw new IllegalStateException(e); }
    }

    static String identity(List<Segment> segments) {
        StringBuilder value = new StringBuilder();
        for (Segment part : segments) {
            value.append(URI.create(part.url()).getRawPath()).append('|');
            if (part.keyUrl() != null) value.append(URI.create(part.keyUrl()).getRawPath());
            value.append('|').append(Arrays.toString(part.iv())).append('|').append(part.duration()).append('\n');
        }
        return sha256(value.toString().getBytes(StandardCharsets.UTF_8));
    }
}
