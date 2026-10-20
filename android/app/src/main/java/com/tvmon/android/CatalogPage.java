package com.tvmon.android;

import java.io.IOException;
import java.net.URI;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Reads #other_list with the same depth rules as the desktop HTMLParser. */
final class CatalogPage {
    final List<SiteClient.Item> items = new ArrayList<>();
    final Set<Integer> pages = new HashSet<>();
    boolean complete;
    private static final Set<String> VOID = new HashSet<>(Arrays.asList("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"));
    private static final Pattern TOKEN = Pattern.compile("(?s)<!--.*?-->|<![^>]*>|<(?:\"[^\"]*\"|'[^']*'|[^'\">])*>|[^<]+");
    private static final Pattern ATTR = Pattern.compile("([\\w:-]+)(?:\\s*=\\s*(?:\"([^\"]*)\"|'([^']*)'|([^\\s>]+)))?");

    CatalogPage(SiteClient client, String content, String base, String html) throws Exception {
        html = html.replaceAll("(?is)<(?:script|style)\\b[^>]*>.*?</(?:script|style)\\s*>", "");
        List<String> stack = new ArrayList<>();
        int listDepth = 0, rowDepth = 0, titleDepth = 0, dateDepth = 0;
        StringBuilder title = new StringBuilder(), date = new StringBuilder();
        String url = "", thumbnail = "";
        Matcher tokens = TOKEN.matcher(html);
        while (tokens.find()) {
            String token = tokens.group();
            if (token.startsWith("<!")) continue;
            if (!token.startsWith("<")) {
                if (rowDepth > 0) { if (titleDepth > 0) title.append(token); if (dateDepth > 0) date.append(token); }
                continue;
            }
            Matcher tagMatch = Pattern.compile("^<\\s*(/?)\\s*([a-zA-Z0-9]+)").matcher(token);
            if (!tagMatch.find()) continue;
            String tag = tagMatch.group(2).toLowerCase(java.util.Locale.ROOT);
            if (!tagMatch.group(1).isEmpty()) {
                int depth = stack.lastIndexOf(tag) + 1;
                if (depth == 0) continue;
                if (rowDepth > 0 && depth == rowDepth) {
                    if (SiteClient.text(title.toString()).isEmpty() || url.isEmpty()) throw new IOException("회차 이름 또는 링크를 읽지 못했습니다.");
                    items.add(new SiteClient.Item(SiteClient.text(title.toString()), url, SiteClient.text(date.toString()), thumbnail));
                    rowDepth = titleDepth = dateDepth = 0;
                }
                if (depth == titleDepth) titleDepth = 0;
                if (depth == dateDepth) dateDepth = 0;
                if (depth == listDepth) { complete = true; listDepth = 0; }
                stack.subList(depth - 1, stack.size()).clear(); continue;
            }
            Map<String, String> attrs = new HashMap<>(); Matcher attributes = ATTR.matcher(token.substring(tagMatch.end()));
            while (attributes.find()) attrs.put(attributes.group(1).toLowerCase(java.util.Locale.ROOT), attributes.group(2) != null ? attributes.group(2) : attributes.group(3) != null ? attributes.group(3) : attributes.group(4) != null ? attributes.group(4) : "");
            if (!VOID.contains(tag)) stack.add(tag);
            int depth = stack.size();
            Set<String> classes = new HashSet<>(Arrays.asList(attrs.getOrDefault("class", "").split("\\s+")));
            if ("other_list".equals(attrs.get("id"))) listDepth = depth;
            if (listDepth == 0) continue;
            if ("li".equals(tag) && classes.contains("searchText")) {
                if (rowDepth > 0) throw new IOException("회차 목록 형식이 올바르지 않습니다.");
                rowDepth = depth; title.setLength(0); date.setLength(0); url = thumbnail = "";
            }
            if ("a".equals(tag) && classes.contains("pg_page")) {
                String target = SiteClient.resolve(base, SiteClient.text(attrs.getOrDefault("href", "")));
                if (!client.canonical(target, base, false).equals(content)) throw new IOException("다른 작품의 페이지가 포함되어 있습니다.");
                Matcher number = Pattern.compile("(?:^|&)page=([0-9]+)(?:&|$)").matcher(URI.create(target).getQuery() == null ? "" : URI.create(target).getQuery());
                if (!number.find() || Integer.parseInt(number.group(1)) < 1) throw new IOException("회차 목록의 페이지 번호가 올바르지 않습니다.");
                pages.add(Integer.parseInt(number.group(1)));
            }
            if (rowDepth == 0) continue;
            if ("a".equals(tag) && classes.contains("title")) {
                url = client.canonical(SiteClient.text(attrs.getOrDefault("href", "")), base, true);
                if (!url.substring(0, url.lastIndexOf('/')).equals(content)) throw new IOException("다른 작품의 회차가 포함되어 있습니다.");
                titleDepth = depth;
            }
            if ("img".equals(tag)) {
                thumbnail = attrs.getOrDefault("data-src", attrs.getOrDefault("data-original", attrs.getOrDefault("src", "")));
                try { thumbnail = SiteClient.resolve(base, thumbnail); SiteClient.checked(thumbnail); } catch (IOException e) { thumbnail = ""; }
            }
            if (classes.contains("date")) dateDepth = depth;
        }
    }
}
