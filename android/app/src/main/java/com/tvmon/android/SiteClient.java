package com.tvmon.android;

import android.text.Html;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.InterruptedIOException;
import java.net.HttpURLConnection;
import java.net.IDN;
import java.net.URI;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.LongConsumer;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

final class SiteClient {
    static final String CANONICAL = "https://tvmon1.com";
    static final String PLAYER = "https://player.bunny-frame.online/";
    static final String DEFAULT_DOMAINS = "https://tvmon1.com\nhttps://tvwiki52.net";
    static final String GUIDE = "https://tvwikiurl.net/";
    static final int MEDIA_LIMIT = 32 * 1024 * 1024;
    static final String AGENT = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36";
    private final List<String> domains;
    private String connectedDomain = "";

    record Response(byte[] body, String url) {}
    record Item(String title, String url, String meta, String thumbnail) {}
    record Search(List<Item> items, boolean more) {}
    record Video(String title, String manifest) {}
    private static final class ConnectionFailure extends IOException {
        ConnectionFailure(IOException cause) { super(cause.getMessage(), cause); }
    }

    static final class Stop {
        private final AtomicBoolean stopped = new AtomicBoolean();
        private final Set<HttpURLConnection> connections = new HashSet<>();
        void check() throws InterruptedIOException {
            if (stopped.get() || Thread.currentThread().isInterrupted()) throw new InterruptedIOException("작업을 중단했습니다.");
        }
        synchronized void attach(HttpURLConnection connection) throws IOException { check(); connections.add(connection); }
        synchronized void detach(HttpURLConnection connection) { connections.remove(connection); }
        synchronized void cancel() { stopped.set(true); for (HttpURLConnection c : connections) c.disconnect(); connections.clear(); }
        boolean stopped() { return stopped.get(); }
    }

    SiteClient(List<String> domains) { this.domains = new ArrayList<>(domains); }
    String connectedDomain() { return connectedDomain; }

    static String origin(String value) throws IOException {
        try {
            String input = value.trim();
            if (!input.contains("://")) input = "https://" + input;
            URI uri = new URI(input);
            String host = uri.getHost();
            if (host == null && uri.getRawAuthority() != null && !uri.getRawAuthority().contains(":")) host = IDN.toASCII(uri.getRawAuthority());
            if (!"https".equals(uri.getScheme()) || host == null || uri.getUserInfo() != null
                    || (uri.getPort() != -1 && uri.getPort() != 443) || uri.getQuery() != null || uri.getFragment() != null
                    || (!uri.getPath().isEmpty() && !"/".equals(uri.getPath()))
                    || !host.matches("(?i)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?")) throw new IllegalArgumentException();
            return "https://" + host.toLowerCase(Locale.ROOT);
        } catch (Exception e) { throw new IOException("도메인은 https://tvmon1.com 형태로 입력해 주세요.", e); }
    }

    static List<String> domains(String text) throws IOException {
        LinkedHashSet<String> result = new LinkedHashSet<>();
        for (String part : text.split("[\\s,]+")) if (!part.isEmpty()) result.add(origin(part));
        if (result.isEmpty()) throw new IOException("접속할 도메인을 하나 이상 입력해 주세요.");
        return new ArrayList<>(result);
    }

    static String resolve(String base, String value) throws IOException {
        try { return URI.create(base).resolve(value).toString(); }
        catch (Exception e) { throw new IOException("서버에서 올바르지 않은 주소를 받았습니다.", e); }
    }
    static URI checked(String value) throws IOException {
        try {
            URI uri = new URI(value);
            boolean localTest = BuildConfig.DEBUG && "http".equals(uri.getScheme()) && "127.0.0.1".equals(uri.getHost());
            if ((!"https".equals(uri.getScheme()) && !localTest) || uri.getHost() == null || uri.getUserInfo() != null
                    || value.matches("(?s).*[\\x00-\\x20\\x7f].*") || uri.getPort() == 0) throw new IllegalArgumentException();
            return uri;
        } catch (Exception e) { throw new IOException("올바른 HTTPS 주소가 아닙니다.", e); }
    }

    static Response read(String url, String referer, int limit, Stop stop) throws IOException {
        return read(url, referer, limit, stop, null);
    }

    static Response read(String url, String referer, int limit, Stop stop, LongConsumer received) throws IOException {
        for (int redirect = 0; redirect <= 5; redirect++) {
            stop.check();
            URI uri = checked(url);
            HttpURLConnection connection = (HttpURLConnection) uri.toURL().openConnection();
            try {
                stop.attach(connection);
                connection.setConnectTimeout(12000); connection.setReadTimeout(12000);
                connection.setInstanceFollowRedirects(false);
                connection.setRequestProperty("User-Agent", AGENT);
                connection.setRequestProperty("Referer", referer);
                URI ref = checked(referer);
                connection.setRequestProperty("Origin", ref.getScheme() + "://" + ref.getRawAuthority());
                connection.setRequestProperty("Accept", "*/*");
                connection.setRequestProperty("Accept-Encoding", "identity");
                int status;
                try { status = connection.getResponseCode(); }
                catch (IOException error) { stop.check(); throw new ConnectionFailure(error); }
                stop.check();
                if ("challenge".equalsIgnoreCase(connection.getHeaderField("cf-mitigated")))
                    throw new IOException("사이트에서 브라우저 인증을 요청했습니다. 이 영상 전용 버전은 브라우저 인증을 지원하지 않습니다.");
                if (status == 301 || status == 302 || status == 303 || status == 307 || status == 308) {
                    String next = connection.getHeaderField("Location");
                    if (next == null) throw new IOException("서버의 이동 주소가 없습니다.");
                    url = resolve(url, next); checked(url); continue;
                }
                if (status < 200 || status >= 300) throw new IOException(uri.getHost() + " 서버 응답: HTTP " + status);
                if (connection.getContentLengthLong() > limit) throw new IOException("서버 응답이 허용 크기를 초과했습니다.");
                try (InputStream input = connection.getInputStream(); ByteArrayOutputStream output = new ByteArrayOutputStream()) {
                    byte[] buffer = new byte[65536]; int count;
                    while ((count = input.read(buffer)) != -1) {
                        stop.check();
                        if (count > limit - output.size()) throw new IOException("서버 응답이 허용 크기를 초과했습니다.");
                        output.write(buffer, 0, count);
                        if (received != null) received.accept(count);
                    }
                    long expected = connection.getContentLengthLong();
                    if (expected >= 0 && output.size() != expected) throw new IOException("서버 응답이 완료되기 전에 연결이 끊겼습니다.");
                    return new Response(output.toByteArray(), url);
                }
            } finally { stop.detach(connection); connection.disconnect(); }
        }
        throw new IOException("서버의 주소 이동이 너무 많습니다.");
    }

    private static Response readSite(String url, String referer, int limit, Stop stop) throws IOException {
        try { return read(url, referer, limit, stop); }
        catch (ConnectionFailure original) {
            stop.check();
            if (!"https".equals(checked(url).getScheme())) throw original;
            try {
                Response response = NativeHttp.read(url, referer, limit, stop);
                android.util.Log.i("TVMON", "ECH/DoH 재접속 성공: " + URI.create(response.url()).getHost());
                return response;
            } catch (IOException error) {
                stop.check(); error.addSuppressed(original);
                throw new IOException("기본 연결과 ECH/DoH 재접속이 모두 실패했습니다. " + error.getMessage(), error);
            } catch (LinkageError error) {
                throw new IOException("ECH/DoH 통신 라이브러리를 불러오지 못했습니다.", error);
            }
        }
    }

    boolean isSite(String url) {
        try {
            URI uri = checked(url); String host = uri.getHost().replaceFirst("^www\\.", "");
            if (host.equals("tvmon1.com")) return true;
            for (String domain : domains) if (host.equals(URI.create(domain).getHost().replaceFirst("^www\\.", ""))) return true;
        } catch (IOException ignored) { }
        return false;
    }
    String canonical(String value, String base, boolean episode) throws IOException {
        String url = resolve(base, value);
        URI uri = checked(url);
        if (!isSite(url) || (uri.getPort() != -1 && uri.getPort() != 443)
                || !uri.getPath().matches(episode ? "/[A-Za-z0-9_]+/[0-9]+/[0-9]+/?" : "/[A-Za-z0-9_]+/[0-9]+/?"))
            throw new IOException(episode ? "회차가 포함된 TVMON/TVWIKI 영상 링크를 입력해 주세요." : "올바른 작품 링크가 아닙니다.");
        return CANONICAL + uri.getPath().replaceFirst("/$", "");
    }
    Response fetch(String url, String referer, int limit, Stop stop) throws IOException {
        if (!isSite(url)) return read(url, referer, limit, stop);
        IOException last = null;
        for (String domain : domains) {
            try {
                String target = replaceOrigin(url, domain);
                Response response = readSite(target, isSite(referer) ? replaceOrigin(referer, domain) : referer, limit, stop);
                if (!isSite(response.url())) throw new IOException("등록하지 않은 사이트로 이동했습니다.");
                URI connected = URI.create(response.url()); connectedDomain = origin(connected.getScheme() + "://" + connected.getRawAuthority());
                return response;
            } catch (IOException e) { stop.check(); last = e; }
        }
        throw new IOException("등록된 영상 사이트에 접속하지 못했습니다. 설정에서 현재 도메인을 확인해 주세요.\n" + (last == null ? "" : last.getMessage()), last);
    }
    private static String replaceOrigin(String url, String domain) { URI u = URI.create(url); return domain + u.getRawPath() + (u.getRawQuery() == null ? "" : "?" + u.getRawQuery()); }
    static String text(String value) { return Html.fromHtml(value, Html.FROM_HTML_MODE_LEGACY).toString().replaceAll("[\\s\\u00a0]+", " ").trim(); }
    static String query(String value) {
        try { return URLEncoder.encode(value, "UTF-8"); }
        catch (java.io.UnsupportedEncodingException e) { throw new IllegalStateException(e); }
    }

    Search search(String term, int page, Stop stop) throws Exception {
        IOException last = null;
        for (String domain : domains) {
            stop.check();
            boolean tvmon = URI.create(domain).getHost().replaceFirst("^www\\.", "").equals("tvmon1.com");
            String endpoint = tvmon ? "/bbs/ajax.searchList.php" : "/bbs/search.php";
            String url = domain + endpoint + "?" + (tvmon ? "sfl=wr_subject&sop=and&" : "ajax=1&") + "stx=" + query(term) + "&sst=subIdx&page=" + page;
            try {
                Response response = readSite(url, domain + "/", 8 * 1024 * 1024, stop);
                if (!isSite(response.url()) || !URI.create(response.url()).getPath().equals(endpoint)) throw new IOException("검색 응답 주소가 올바르지 않습니다.");
                Search search = parseSearch(response.body(), response.url(), page);
                connectedDomain = origin(URI.create(response.url()).getScheme() + "://" + URI.create(response.url()).getRawAuthority());
                return search;
            } catch (Exception e) { stop.check(); last = new IOException(domain + ": " + e.getMessage(), e); }
        }
        throw last != null ? last : new IOException("검색할 도메인이 없습니다.");
    }

    Search parseSearch(byte[] body, String base, int page) throws Exception {
        JSONObject payload = new JSONObject(new String(body, StandardCharsets.UTF_8));
        JSONArray rows = payload.getJSONArray("list"); String pages = payload.getString("pages");
        List<Item> result = new ArrayList<>(); Set<String> seen = new HashSet<>();
        for (int i = 0; i < rows.length(); i++) {
            JSONObject row = rows.getJSONObject(i);
            String title = text(row.getString("wr_subject")), url = canonical(row.getString("href"), base, false);
            if (title.isEmpty()) throw new IOException("검색 결과의 작품 이름이 없습니다.");
            if (!seen.add(url)) continue;
            String category = text(row.optString("board_name", "")), year = row.optString("wr_6", "");
            if (year.length() > 4) year = year.substring(0, 4);
            String thumbnail = row.optString("img", "");
            if (thumbnail.isEmpty()) thumbnail = row.optString("ani".equals(row.optString("bo_table")) ? "thumb" : "wr_4", "");
            try { thumbnail = thumbnail.isEmpty() ? "" : resolve(base, thumbnail); checked(thumbnail); } catch (IOException e) { thumbnail = ""; }
            result.add(new Item(title, url, category + (category.isEmpty() || year.isEmpty() ? "" : " · ") + year, thumbnail));
        }
        boolean more = false;
        Matcher matcher = Pattern.compile("[?&]page=([0-9]+)").matcher(pages.replace("&amp;", "&"));
        while (matcher.find()) if (Integer.parseInt(matcher.group(1)) > page) more = true;
        return new Search(result, more);
    }

    List<Item> episodes(String content, Stop stop) throws Exception {
        content = canonical(content, CANONICAL + "/", false);
        TreeSet<Integer> pending = new TreeSet<>(); pending.add(1);
        Set<Integer> visited = new HashSet<>(); Set<String> seen = new HashSet<>(); List<Item> result = new ArrayList<>();
        while (!pending.isEmpty()) {
            stop.check(); int page = pending.pollFirst();
            if (!visited.add(page)) continue;
            Response response = fetch(content + (page == 1 ? "" : "?page=" + page), content, 16 * 1024 * 1024, stop);
            if (!canonical(response.url(), content, false).equals(content)) throw new IOException("회차 목록이 다른 작품으로 이동했습니다.");
            CatalogPage parsed = new CatalogPage(this, content, response.url(), new String(response.body(), StandardCharsets.UTF_8));
            int added = 0;
            for (Item item : parsed.items) if (seen.add(item.url())) { result.add(item); added++; }
            if (!parsed.complete || added == 0) throw new IOException("작품의 전체 회차 목록을 확인하지 못했습니다.");
            for (int next : parsed.pages) if (!visited.contains(next)) pending.add(next);
        }
        Collections.reverse(result); return result;
    }

    Video video(String episode, Stop stop) throws Exception {
        episode = canonical(episode, CANONICAL + "/", true);
        String[] parts = URI.create(episode).getPath().split("/");
        Response response = fetch(CANONICAL + "/bbs/get_episode.php?bo_table=" + parts[1] + "&wr_id=" + parts[2] + "&ep_idx=" + parts[3], episode, 4 * 1024 * 1024, stop);
        JSONObject data = new JSONObject(new String(response.body(), StandardCharsets.UTF_8));
        if (!data.optBoolean("success")) throw new IOException("이 회차에서 영상을 찾지 못했습니다.");
        JSONObject info = data.getJSONObject("episode"); String manifest = info.getString("hls_url"); checked(manifest);
        return new Video(text(info.getString("title")), manifest);
    }

    String currentDomain(Stop stop) throws Exception {
        return currentDomain(GUIDE, stop);
    }
    static String guideUrl(String value) throws IOException {
        String url = value.trim(); if (!url.contains("://")) url = "https://" + url;
        URI uri = checked(url);
        if (!"https".equals(uri.getScheme())) throw new IOException("안내 페이지는 HTTPS 주소로 입력해 주세요.");
        return uri.toString();
    }
    String checkConnection(Stop stop) throws Exception {
        Response response = fetch(CANONICAL + "/", CANONICAL + "/", 8 * 1024 * 1024, stop);
        String html = new String(response.body(), StandardCharsets.UTF_8);
        if (!html.contains("/bbs/") || !(html.contains("TVWIKI") || html.contains("티비위키") || html.contains("TVMON") || html.contains("티비몬"))) throw new IOException("응답은 받았지만 영상 사이트의 홈페이지를 확인하지 못했습니다.");
        return connectedDomain;
    }
    String currentDomain(String guide, Stop stop) throws Exception {
        guide = guideUrl(guide);
        Response response = readSite(guide, guide, 2 * 1024 * 1024, stop);
        if (!URI.create(response.url()).getHost().equals(URI.create(guide).getHost())) throw new IOException("안내 페이지가 다른 사이트로 이동했습니다.");
        String html = new String(response.body(), StandardCharsets.UTF_8);
        String domain = guideDomain(html, response.url());
        Response home;
        try { home = readSite(domain + "/", guide, 8 * 1024 * 1024, stop); }
        catch (IOException error) {
            stop.check();
            throw new IOException("안내 주소 " + domain + "는 찾았지만 영상 사이트에 접속하지 못했습니다. " + error.getMessage(), error);
        }
        String homeText = new String(home.body(), StandardCharsets.UTF_8);
        if (!URI.create(home.url()).getHost().equals(URI.create(domain).getHost())
                || !homeText.contains("/bbs/") || !(homeText.contains("TVWIKI") || homeText.contains("티비위키") || homeText.contains("TVMON") || homeText.contains("티비몬")))
            throw new IOException("안내된 주소에서 영상 사이트를 확인하지 못했습니다.");
        return domain;
    }

    static String guideDomain(String html, String base) throws IOException {
        Matcher rows = Pattern.compile("(?is)<dt\\b[^>]*>(.*?)</dt>\\s*<dd\\b[^>]*>(.*?)</dd>").matcher(html);
        Set<String> candidates = new LinkedHashSet<>();
        while (rows.find()) {
            String label = text(rows.group(1));
            if (label.equals("현재 안내 주소") || label.equals("외부 연결 주소")) candidates.add(origin(text(rows.group(2))));
        }
        if (candidates.isEmpty()) throw new IOException("안내 페이지에서 현재 주소 항목을 찾지 못했습니다. 도메인을 직접 입력해 주세요.");
        if (candidates.size() != 1) throw new IOException("안내 페이지의 주소가 여러 개입니다.");
        String domain = candidates.iterator().next();
        Matcher links = Pattern.compile("(?is)<a\\b[^>]*href\\s*=\\s*['\"]([^'\"]+)['\"]").matcher(html);
        boolean linked = false;
        while (links.find()) {
            try { if (origin(resolve(base, text(links.group(1)))).equals(domain)) linked = true; } catch (IOException ignored) { }
        }
        if (!linked) throw new IOException("안내 주소와 연결 링크가 일치하지 않습니다.");
        return domain;
    }
}
