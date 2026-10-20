package com.tvmon.android;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.app.Dialog;
import android.content.BroadcastReceiver;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.ContentUris;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Color;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.StateListDrawable;
import android.database.Cursor;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.MediaStore;
import android.text.TextUtils;
import android.util.LruCache;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.ViewConfiguration;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.view.Window;
import android.view.WindowManager;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.InputMethodManager;
import android.widget.BaseAdapter;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.ImageButton;
import android.widget.LinearLayout;
import android.widget.ListView;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;
import android.window.OnBackInvokedDispatcher;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class MainActivity extends Activity {
    private static final int BLUE = 0xff245eb8, INK = 0xff20364d, MUTED = 0xff52687e, BACKGROUND = 0xfff3f6fa;
    private static final int SUCCESS = 0xff236646, DANGER = 0xffa13b17, SOFT_BLUE = 0xffeaf0f8, OUTLINE = 0xffcbd5e1, DISABLED = 0xffe3e8ef;
    private static final int SPACE = 4, CARD_RADIUS = 12, TOUCH = 48;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final ExecutorService network = Executors.newFixedThreadPool(3);
    private final LruCache<String, Bitmap> images = new LruCache<String, Bitmap>(8 * 1024 * 1024) {
        @Override protected int sizeOf(String key, Bitmap value) { return value.getByteCount(); }
    };
    private final List<SiteClient.Item> results = new ArrayList<>(), episodes = new ArrayList<>();
    private final Set<String> selected = new HashSet<>(), imageRequests = new HashSet<>();
    private final Set<String> imageFailures = new HashSet<>();
    private final Map<String, DownloadStore.Job> episodeJobs = new HashMap<>();
    private final Button[] navigation = new Button[4];
    private DownloadStore store;
    private FrameLayout content;
    private int tab, page;
    private String term = "", contentUrl = "", contentTitle = "", contentThumbnail = "";
    private boolean more, loading, destroyed;
    private SiteClient.Stop request = new SiteClient.Stop();
    private EditText searchInput;
    private TextView searchStatus, selectedCount;
    private Button searchButton, nextButton, downloadButton, addButton, recentButton, selectAllButton, queuePause, queueStart, queueClear;
    private boolean removingJobs;
    private boolean pausingJob;
    private ProgressBar searchLoading;
    private LinearLayout queueActions;
    private EditText domainInput, guideInput;
    private String draftDomains, draftGuide;
    private BaseAdapter resultAdapter, episodeAdapter, queueAdapter;
    private List<DownloadStore.Job> jobs = new ArrayList<>();
    private TextView queueSummary;
    private final List<SavedVideo> savedVideos = new ArrayList<>();
    private final java.util.Set<String> ambiguousSavedTitles = new java.util.HashSet<>();
    private BaseAdapter libraryAdapter;
    private TextView libraryStatus, libraryEmpty;
    private Button libraryRefresh;
    private Dialog libraryMenuDialog;
    private int libraryRevision;
    private Button siteStatus;
    private SiteClient.Stop connectionStop = new SiteClient.Stop();
    private TextView connectionFeedback;
    private Button connectionButton;
    private int connectionRevision;
    private long lastConnectionCheck;
    private List<SiteClient.Item> recentWorks = new ArrayList<>();
    private final Set<String> recentThumbnailRequests = new HashSet<>();
    private final SiteClient.Stop recentThumbnailStop = new SiteClient.Stop();
    private boolean showingRecent;
    private Dialog detailsDialog;
    private String detailsId;
    private SiteClient.Stop detailsStop;
    private TextView detailsTitle, detailsDuration, detailsState, detailsPercent, detailsSaved, detailsSpeed, detailsEta, detailsCount, detailsGrouping, detailsWorkers, detailsMessage;
    private Button detailsAction, detailsWorkersButton;
    private SegmentMap detailsMap;
    private ScrollView detailsScroll;
    private int detailsScrollPosition;
    private boolean detailsMetadataFailed, detailsWorkersExpanded, detailsPartsExpanded, detailsMenuShowing;
    private LinearLayout detailsContent, detailsParts, detailsMetrics, detailsMenu;
    private Button detailsPartsButton, detailsMenuButton;
    private TextView detailsHeading;
    private String detailsLayoutMode = "";
    private final Runnable refreshDetails = new Runnable() {
        @Override public void run() { if (detailsDialog != null && detailsDialog.isShowing()) { updateDetails(); handler.postDelayed(this, 500); } }
    };
    private final Runnable refreshQueue = () -> {
        if (tab == 1 && queueAdapter != null) updateQueue();
        else if (tab == 0 && !contentUrl.isEmpty() && episodeAdapter != null) { updateEpisodeStates(); episodeAdapter.notifyDataSetChanged(); }
    };
    private final BroadcastReceiver receiver = new BroadcastReceiver() {
        @Override public void onReceive(Context context, Intent intent) { handler.removeCallbacks(refreshQueue); handler.postDelayed(refreshQueue, 150); }
    };

    // Android 10–12 use a signature-protected receiver; Android 13+ use NOT_EXPORTED.
    @android.annotation.SuppressLint("UnspecifiedRegisterReceiverFlag")
    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        store = DownloadStore.get(this); TransferScheduler.channel(this);
        if (state != null) {
            term = state.getString("term", ""); tab = state.getInt("tab", 0);
            contentUrl = state.getString("content", ""); contentTitle = state.getString("title", "회차 선택");
            contentThumbnail = state.getString("thumbnail", "");
            showingRecent = state.getBoolean("recent", false);
            draftDomains = state.getString("draftDomains"); draftGuide = state.getString("draftGuide");
            ArrayList<String> saved = state.getStringArrayList("selected"); if (saved != null) selected.addAll(saved);
        } else showingRecent = true;
        if (Build.VERSION.SDK_INT >= 30) getWindow().setDecorFitsSystemWindows(false);
        LinearLayout root = column(); root.setBackgroundColor(BACKGROUND);
        root.setOnApplyWindowInsetsListener((view, insets) -> {
            if (Build.VERSION.SDK_INT >= 30) {
                android.graphics.Insets bars = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
                int keyboard = insets.getInsets(WindowInsets.Type.ime()).bottom;
                view.setPadding(bars.left, bars.top, bars.right, Math.max(bars.bottom, keyboard));
            } else view.setPadding(insets.getSystemWindowInsetLeft(), insets.getSystemWindowInsetTop(), insets.getSystemWindowInsetRight(), insets.getSystemWindowInsetBottom());
            return insets;
        });
        LinearLayout brand = row(); brand.setGravity(Gravity.CENTER_VERTICAL); brand.setPadding(dp(20), dp(12), dp(20), dp(12));
        TextView logo = text("TVMON", 22, INK, true); logo.setSingleLine(true); brand.addView(logo);
        brand.addView(new View(this), new LinearLayout.LayoutParams(0, 1, 1));
        siteStatus = button("사이트 확인 중…", false); siteStatus.setTextSize(12); siteStatus.setMaxWidth(Math.min(dp(230), dp(getResources().getConfiguration().screenWidthDp - 52) - (int) Math.ceil(logo.getPaint().measureText("TVMON")))); siteStatus.setMaxLines(2); siteStatus.setContentDescription("사이트 접속 상태, 누르면 설정으로 이동"); siteStatus.setOnClickListener(v -> showTab(2)); brand.addView(siteStatus);
        siteStatus.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE);
        root.addView(brand);
        content = new FrameLayout(this); root.addView(content, new LinearLayout.LayoutParams(-1, 0, 1));
        LinearLayout bottom = row(); bottom.setPadding(dp(12), dp(6), dp(12), dp(6)); bottom.setBackgroundColor(Color.WHITE);
        String[] labels = {"검색", "대기열", "설정", "보관함"};
        for (final int destination : new int[]{0, 1, 3, 2}) {
            Button button = button(labels[destination], false); button.setSingleLine(true); button.setPadding(dp(2), dp(8), dp(2), dp(8)); button.setMinHeight(dp(48)); button.setMinimumHeight(dp(48)); navigation[destination] = button;
            bottom.addView(button, weighted()); button.setOnClickListener(v -> showTab(destination));
        }
        root.addView(bottom); setContentView(root); root.requestApplyInsets();
        if (Build.VERSION.SDK_INT >= 33) getOnBackInvokedDispatcher().registerOnBackInvokedCallback(OnBackInvokedDispatcher.PRIORITY_DEFAULT, this::back);
        if (Build.VERSION.SDK_INT >= 33) registerReceiver(receiver, new IntentFilter(DownloadStore.CHANGED), Context.RECEIVER_NOT_EXPORTED);
        else registerReceiver(receiver, new IntentFilter(DownloadStore.CHANGED), "com.tvmon.android.INTERNAL", null);
        showTab(tab);
        if (state != null && tab == 1) {
            DownloadStore.Job job = store.find(state.getString("details", ""));
            if (job != null) content.post(() -> showJobDetails(job));
        }
        if (state != null && tab == 0) {
            if (!contentUrl.isEmpty()) loadEpisodes(new SiteClient.Item(contentTitle, contentUrl, "", ""));
            else if (!term.isEmpty()) search(false);
        }
        checkSiteConnection(null, null);
    }

    private SiteClient client() throws Exception { return new SiteClient(SiteClient.domains(settings().getString("domains", SiteClient.DEFAULT_DOMAINS))); }
    private SharedPreferences settings() { return getSharedPreferences("settings", 0); }
    private SiteClient.Stop beginRequest() { request.cancel(); request = new SiteClient.Stop(); return request; }
    private boolean current(SiteClient.Stop stop) { return !destroyed && !stop.stopped() && request == stop; }

    private void showTab(int destination) {
        if (searchInput != null && tab == 0 && contentUrl.isEmpty()) term = searchInput.getText().toString();
        rememberSettingsDraft();
        tab = destination; content.removeAllViews();
        for (int i = 0; i < navigation.length; i++) {
            navigation[i].setBackground(background(i == tab ? 0xffe5edfa : Color.WHITE, 12));
            navigation[i].setTextColor(i == tab ? BLUE : MUTED);
            navigation[i].setSelected(i == tab);
        }
        if (tab == 0) { if (contentUrl.isEmpty()) showSearch(); else showEpisodes(); }
        else if (tab == 1) showQueue();
        else if (tab == 3) showLibrary();
        else showSettings();
    }

    private LinearLayout page(String title, String subtitle) {
        LinearLayout layout = column(); layout.setPadding(dp(20), dp(8), dp(20), 0);
        TextView heading = text(title, 26, INK, true); heading.setAccessibilityHeading(true); heading.setPadding(0, dp(4), 0, dp(8)); layout.addView(heading);
        TextView helper = text(subtitle, 14, MUTED, false); helper.setPadding(0, 0, 0, dp(16)); layout.addView(helper);
        content.addView(layout, new FrameLayout.LayoutParams(-1, -1)); return layout;
    }
    private void showSearch() {
        LinearLayout layout = page("작품 찾기", "제목으로 검색하고 원하는 회차를 저장하세요.");
        LinearLayout form = row(); form.setGravity(Gravity.CENTER_VERTICAL);
        searchInput = input("작품 제목 또는 영상 링크"); searchInput.setSingleLine(true); searchInput.setText(term);
        searchInput.setImeOptions(EditorInfo.IME_ACTION_SEARCH);
        int controlHeight = Math.max(dp(52), (int) Math.ceil(searchInput.getPaint().getFontMetrics().bottom - searchInput.getPaint().getFontMetrics().top) + searchInput.getPaddingTop() + searchInput.getPaddingBottom());
        form.addView(searchInput, new LinearLayout.LayoutParams(0, controlHeight, 1));
        searchButton = button("검색", true); LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(dp(76), controlHeight); params.leftMargin = dp(8); form.addView(searchButton, params);
        layout.addView(form);
        searchButton.setOnClickListener(v -> search(false));
        searchInput.setOnEditorActionListener((v, action, event) -> { if (action == EditorInfo.IME_ACTION_SEARCH) { search(false); return true; } return false; });
        recentWorks = store.recentWorks();
        recentButton = null;
        if (!recentWorks.isEmpty()) {
            Button history = button("최근 받은 작품", false); LinearLayout.LayoutParams historyParams = new LinearLayout.LayoutParams(-1, dp(48)); historyParams.topMargin = dp(12); layout.addView(history, historyParams);
            recentButton = history; history.setVisibility(showingRecent ? View.GONE : View.VISIBLE);
            history.setOnClickListener(v -> { beginRequest(); loading = false; showingRecent = true; results.clear(); more = false; term = ""; content.removeAllViews(); showSearch(); });
        }
        searchStatus = text(loading ? "작품을 검색하고 있습니다…" : showingRecent && !recentWorks.isEmpty() ? "최근 받은 작품 · 누르면 회차 보기, 좌우로 밀면 목록 삭제" : results.isEmpty() ? (showingRecent ? "제목을 입력해 작품을 찾아보세요." : "검색 결과가 없습니다. 다른 제목을 입력해 보세요.") : results.size() + "개 작품", 14, MUTED, false);
        searchStatus.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE);
        LinearLayout feedback = row(); feedback.setGravity(Gravity.CENTER_VERTICAL); feedback.setPadding(0, dp(12), 0, dp(12));
        searchLoading = new ProgressBar(this); searchLoading.setVisibility(loading ? View.VISIBLE : View.GONE); LinearLayout.LayoutParams spinner = new LinearLayout.LayoutParams(dp(24), dp(24)); spinner.rightMargin = dp(12); feedback.addView(searchLoading, spinner); feedback.addView(searchStatus, new LinearLayout.LayoutParams(0, -2, 1)); layout.addView(feedback);
        ListView list = list(layout); resultAdapter = new BaseAdapter() {
            public int getCount() { return showingRecent ? recentWorks.size() : results.size(); }
            public Object getItem(int position) { return showingRecent ? recentWorks.get(position) : results.get(position); }
            public long getItemId(int position) { return position; }
            public View getView(int position, View recycled, ViewGroup parent) { return itemRow((SiteClient.Item) getItem(position), false, recycled); }
        };
        list.setAdapter(resultAdapter); list.setOnItemClickListener((parent, view, position, id) -> { selected.clear(); loadEpisodes((SiteClient.Item) parent.getItemAtPosition(position)); });
        swipeToRemove(list, item -> { if (!showingRecent || !(item instanceof SiteClient.Item work)) return null; return () -> removeRecentWork(work); });
        list.setOnItemLongClickListener((parent, view, position, id) -> {
            Object item = parent.getItemAtPosition(position); if (!showingRecent || !(item instanceof SiteClient.Item work)) return false;
            confirmRemoval("최근 받은 작품에서 삭제할까요?", work.title() + "\n다운로드 대기열과 저장된 영상은 유지됩니다.", "목록에서 삭제", () -> removeRecentWork(work)); return true;
        });
        nextButton = button("검색 결과 더 보기", false); nextButton.setVisibility(more ? View.VISIBLE : View.GONE); nextButton.setOnClickListener(v -> search(true)); list.addFooterView(nextButton, null, false);
        searchButton.setEnabled(!loading);
        if (showingRecent) loadRecentThumbnails();
    }

    private void loadRecentThumbnails() {
        List<SiteClient.Item> missing = new ArrayList<>();
        for (SiteClient.Item item : recentWorks) if (item.thumbnail().isEmpty() && recentThumbnailRequests.add(item.url())) missing.add(item);
        if (missing.isEmpty()) return;
        network.execute(() -> {
            for (SiteClient.Item work : missing) {
                if (recentThumbnailStop.stopped()) return;
                try {
                    SiteClient api = client();
                    for (int page = 1; ; page++) {
                        SiteClient.Search found = api.search(work.title(), page, recentThumbnailStop);
                        SiteClient.Item match = null;
                        for (SiteClient.Item item : found.items()) if (work.url().equals(item.url())) { match = item; break; }
                        if (match != null) {
                            store.rememberThumbnail(match);
                            handler.post(() -> {
                                if (destroyed) return;
                                recentWorks = store.recentWorks();
                                if (tab == 0 && contentUrl.isEmpty() && showingRecent && resultAdapter != null) resultAdapter.notifyDataSetChanged();
                            });
                            break;
                        }
                        if (!found.more()) break;
                    }
                } catch (Exception error) { if (!recentThumbnailStop.stopped()) android.util.Log.w("TVMON", "최근 작품 썸네일 조회 실패", error); }
            }
        });
    }

    private void search(boolean next) {
        if (destroyed || loading) return;
        String value = searchInput.getText().toString().trim(); if (value.isEmpty()) { searchInput.setError("검색할 제목을 입력해 주세요."); return; }
        ((InputMethodManager) getSystemService(INPUT_METHOD_SERVICE)).hideSoftInputFromWindow(searchInput.getWindowToken(), 0);
        if (value.startsWith("https://")) {
            try {
                SiteClient api = client();
                String episode = api.canonical(value, SiteClient.CANONICAL + "/", true);
                queueItems(java.util.Collections.singletonList(new SiteClient.Item("영상 " + Uri.parse(episode).getLastPathSegment(), episode, "", "")), true); return;
            } catch (Exception ignored) { }
            try { loadEpisodes(new SiteClient.Item("회차 선택", client().canonical(value, SiteClient.CANONICAL + "/", false), "", "")); }
            catch (Exception e) { error(e); }
            return;
        }
        showingRecent = false; resultAdapter.notifyDataSetChanged();
        if (recentButton != null) recentButton.setVisibility(View.VISIBLE);
        term = value; loading = true; searchButton.setEnabled(false); nextButton.setEnabled(false);
        if (!next) { results.clear(); resultAdapter.notifyDataSetChanged(); }
        searchLoading.setVisibility(View.VISIBLE);
        searchStatus.setText("작품을 검색하고 있습니다…");
        int wantedPage = next ? page + 1 : 1; SiteClient.Stop stop = beginRequest();
        network.execute(() -> {
            try {
                SiteClient api = client(); SiteClient.Search found = api.search(value, wantedPage, stop);
                handler.post(() -> {
                    if (!current(stop)) return;
                    markConnected(api.connectedDomain());
                    loading = false; page = wantedPage; more = found.more();
                    if (!next) results.clear();
                    Set<String> seen = new HashSet<>(); for (SiteClient.Item item : results) seen.add(item.url());
                    for (SiteClient.Item item : found.items()) if (seen.add(item.url())) results.add(item);
                    if (tab == 0 && contentUrl.isEmpty()) { resultAdapter.notifyDataSetChanged(); searchLoading.setVisibility(View.GONE); searchStatus.setText(results.isEmpty() ? "검색 결과가 없습니다. 다른 제목을 입력해 보세요." : results.size() + "개 작품"); searchButton.setEnabled(true); nextButton.setEnabled(true); nextButton.setVisibility(more ? View.VISIBLE : View.GONE); }
                });
            } catch (Exception e) { handler.post(() -> { if (current(stop)) { markConnectionFailure(e); loading = false; if (tab == 0 && contentUrl.isEmpty()) { searchLoading.setVisibility(View.GONE); searchButton.setEnabled(true); nextButton.setEnabled(true); searchStatus.setText("검색하지 못했습니다. 다시 검색해 주세요.\n" + DownloadEngine.message(e)); } } }); }
        });
    }

    private void loadEpisodes(SiteClient.Item item) {
        if (searchInput != null && contentUrl.isEmpty()) term = searchInput.getText().toString();
        contentUrl = item.url(); contentTitle = item.title(); contentThumbnail = item.thumbnail(); episodes.clear(); loading = true;
        SiteClient.Stop stop = beginRequest(); content.removeAllViews(); showEpisodes();
        network.execute(() -> {
            try {
                SiteClient api = client(); List<SiteClient.Item> found = api.episodes(item.url(), stop);
                handler.post(() -> { if (current(stop)) { markConnected(api.connectedDomain()); episodes.addAll(found); loading = false; if (tab == 0) { episodeAdapter.notifyDataSetChanged(); updateSelection(); } } });
            } catch (Exception e) { handler.post(() -> { if (current(stop)) { markConnectionFailure(e); loading = false; if (tab == 0) { updateSelection(); selectedCount.setText(DownloadEngine.message(e)); } } }); }
        });
    }

    private void showEpisodes() {
        updateEpisodeStates();
        LinearLayout layout = page(contentTitle, "회차를 선택해 대기열에 추가하거나 바로 받으세요.");
        Button back = button(showingRecent ? "‹ 최근 받은 작품" : "‹ 검색 결과", false); back.setBackground(selectableBackground(Color.TRANSPARENT, 0)); back.setGravity(Gravity.START | Gravity.CENTER_VERTICAL); back.setOnClickListener(v -> back()); layout.addView(back, new LinearLayout.LayoutParams(-2, -2));
        LinearLayout tools = row(); tools.setGravity(Gravity.CENTER_VERTICAL);
        selectedCount = text("", 13, MUTED, false); tools.addView(selectedCount, new LinearLayout.LayoutParams(0, -2, 1));
        selectedCount.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE);
        Button all = button("전체 선택", false); selectAllButton = all; all.setOnClickListener(v -> {
            if (selected.size() == episodes.size()) selected.clear(); else for (SiteClient.Item item : episodes) selected.add(item.url());
            episodeAdapter.notifyDataSetChanged(); updateSelection();
        }); tools.addView(all); layout.addView(tools);
        ListView list = list(layout); episodeAdapter = new BaseAdapter() {
            public int getCount() { return episodes.size(); }
            public Object getItem(int position) { return episodes.get(position); }
            public long getItemId(int position) { return position; }
            public View getView(int position, View recycled, ViewGroup parent) { return itemRow(episodes.get(position), true, recycled); }
        };
        list.setAdapter(episodeAdapter);
        list.setOnItemClickListener((parent, view, position, id) -> { String url = ((SiteClient.Item) parent.getItemAtPosition(position)).url(); if (!selected.remove(url)) selected.add(url); episodeAdapter.notifyDataSetChanged(); updateSelection(); });
        LinearLayout actions = row(); actions.setPadding(0, dp(8), 0, dp(12));
        addButton = button("대기열 추가", false); downloadButton = button("다운로드", true);
        actions.addView(addButton, weighted()); actions.addView(downloadButton, weighted()); layout.addView(actions);
        addButton.setOnClickListener(v -> queueSelected(false)); downloadButton.setOnClickListener(v -> queueSelected(true)); updateSelection();
    }
    private void updateSelection() {
        selectedCount.setText(loading ? "전체 회차를 불러오고 있습니다…" : episodes.size() + "개 회차 · " + selected.size() + "개 선택");
        addButton.setEnabled(!loading && !selected.isEmpty()); downloadButton.setEnabled(!loading && !selected.isEmpty());
        selectAllButton.setText(!episodes.isEmpty() && selected.size() == episodes.size() ? "선택 해제" : "전체 선택"); selectAllButton.setEnabled(!loading && !episodes.isEmpty());
    }
    private void queueSelected(boolean start) {
        List<SiteClient.Item> chosen = new ArrayList<>(); for (SiteClient.Item item : episodes) if (selected.contains(item.url())) chosen.add(item);
        queueItems(chosen, start);
    }
    private void queueItems(List<SiteClient.Item> items, boolean start) {
        try {
            int count = store.add(items, contentUrl.isEmpty() ? null : new SiteClient.Item(contentTitle, contentUrl, "", contentThumbnail)); selected.clear();
            Toast.makeText(this, count > 0 ? count + "개 회차를 추가했습니다." : "이미 대기열에 있는 회차입니다.", Toast.LENGTH_SHORT).show();
            showTab(1); if (start) startDownloads();
        } catch (Exception e) { error(e); }
    }

    private static final class ItemViews {
        LinearLayout card; TextView title, meta, status; CheckBox check; ImageView cover; String url;
    }
    private View itemRow(SiteClient.Item item, boolean selectable, View recycled) {
        ItemViews views;
        if (recycled != null && recycled.getTag() instanceof ItemViews holder && (holder.check != null) == selectable) views = holder;
        else {
            views = new ItemViews(); views.card = row(); views.card.setGravity(Gravity.CENTER_VERTICAL); views.card.setPadding(dp(12), dp(selectable ? 8 : 12), dp(12), dp(selectable ? 8 : 12)); views.card.setMinimumHeight(dp(64)); views.card.setTag(views);
            if (selectable) {
                views.check = new CheckBox(this); views.check.setClickable(false); views.check.setFocusable(false); views.check.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_NO); views.check.setButtonTintList(android.content.res.ColorStateList.valueOf(BLUE));
                views.card.addView(views.check, new LinearLayout.LayoutParams(dp(40), dp(48)));
                views.card.setAccessibilityDelegate(new View.AccessibilityDelegate() {
                    @Override public void onInitializeAccessibilityNodeInfo(View host, android.view.accessibility.AccessibilityNodeInfo info) { super.onInitializeAccessibilityNodeInfo(host, info); ItemViews bound = (ItemViews) host.getTag(); info.setClassName(CheckBox.class.getName()); info.setCheckable(true); info.setChecked(selected.contains(bound.url)); }
                });
            } else {
                views.cover = new ImageView(this); views.cover.setScaleType(ImageView.ScaleType.CENTER_CROP); views.cover.setBackground(background(SOFT_BLUE, 8)); views.cover.setClipToOutline(true); views.cover.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_NO);
                LinearLayout.LayoutParams size = new LinearLayout.LayoutParams(dp(60), dp(80)); size.rightMargin = dp(12); views.card.addView(views.cover, size);
            }
            LinearLayout labels = column(); views.title = text("", 16, INK, true); views.title.setMaxLines(2); views.title.setEllipsize(TextUtils.TruncateAt.END); labels.addView(views.title);
            views.meta = text("", 14, MUTED, false); views.meta.setPadding(0, dp(4), 0, 0); views.meta.setMaxLines(2); views.meta.setEllipsize(TextUtils.TruncateAt.END); labels.addView(views.meta);
            views.status = text("", 14, SUCCESS, true); views.status.setPadding(0, dp(4), 0, 0); labels.addView(views.status); views.card.addView(labels, new LinearLayout.LayoutParams(0, -2, 1));
        }
        views.url = item.url(); views.title.setText(item.title()); views.meta.setText(item.meta().isEmpty() ? "회차 보기 ›" : item.meta()); views.meta.setVisibility(selectable && item.meta().isEmpty() ? View.GONE : View.VISIBLE);
        DownloadStore.Job job = selectable ? episodeJobs.get(item.url()) : null; String status = job == null ? "" : DownloadStore.DONE.equals(job.state) ? "다운로드 완료" : jobStatus(job);
        views.status.setText(status); views.status.setTextColor(job == null ? MUTED : stateColor(job)); views.status.setVisibility(job == null ? View.GONE : View.VISIBLE);
        boolean checked = selectable && selected.contains(item.url()); views.card.setBackground(selectableBackground(checked ? 0xffe5edfa : Color.WHITE, checked ? BLUE : 0));
        if (views.check != null) views.check.setChecked(checked); else loadImage(item.thumbnail(), views.cover);
        if (!selectable) dismissAction(views.card, showingRecent ? "최근 받은 작품에서 삭제" : null, () -> removeRecentWork(item));
        views.card.setContentDescription(item.title() + (selectable ? status.isEmpty() ? "" : ", " + status : ", " + views.meta.getText())); return views.card;
    }

    private void removeRecentWork(SiteClient.Item work) {
        try {
            store.removeRecentWork(work.url()); recentWorks = store.recentWorks(); resultAdapter.notifyDataSetChanged();
            if (recentWorks.isEmpty()) { if (recentButton != null) recentButton.setVisibility(View.GONE); searchStatus.setText("최근 받은 작품이 없습니다. 제목을 입력해 작품을 찾아보세요."); }
            Toast.makeText(this, "최근 받은 작품에서 삭제했습니다. 영상은 보관됩니다.", Toast.LENGTH_SHORT).show();
        } catch (Exception e) { error(e); }
    }

    private void dismissAction(View card, String label, Runnable action) {
        card.setAccessibilityDelegate(label == null ? null : new View.AccessibilityDelegate() {
            @Override public void onInitializeAccessibilityNodeInfo(View host, android.view.accessibility.AccessibilityNodeInfo info) { super.onInitializeAccessibilityNodeInfo(host, info); info.addAction(new android.view.accessibility.AccessibilityNodeInfo.AccessibilityAction(android.view.accessibility.AccessibilityNodeInfo.ACTION_DISMISS, label)); }
            @Override public boolean performAccessibilityAction(View host, int id, Bundle args) { if (id == android.view.accessibility.AccessibilityNodeInfo.ACTION_DISMISS) { action.run(); return true; } return super.performAccessibilityAction(host, id, args); }
        });
    }

    // Keep taps and vertical scrolling in ListView; claim only deliberate horizontal drags.
    @android.annotation.SuppressLint("ClickableViewAccessibility") // Dismiss also has a labeled accessibility action and a long-press menu.
    private void swipeToRemove(ListView list, java.util.function.Function<Object, Runnable> removal) {
        list.setOnTouchListener(new View.OnTouchListener() {
            float downX, downY; boolean dragging, vertical; View card; Runnable action;
            final int slop = ViewConfiguration.get(MainActivity.this).getScaledTouchSlop();
            @Override public boolean onTouch(View view, MotionEvent event) {
                int type = event.getActionMasked();
                if (type == MotionEvent.ACTION_DOWN) {
                    reset(); downX = event.getX(); downY = event.getY();
                    int position = list.pointToPosition((int) downX, (int) downY);
                    if (position != ListView.INVALID_POSITION) { action = removal.apply(list.getItemAtPosition(position)); if (action != null) card = list.getChildAt(position - list.getFirstVisiblePosition()); }
                    return false;
                }
                if (card == null) return false;
                if (type == MotionEvent.ACTION_POINTER_DOWN) { boolean consumed = dragging; reset(); return consumed; }
                float dx = event.getX() - downX, dy = event.getY() - downY;
                if (type == MotionEvent.ACTION_MOVE && !vertical) {
                    if (!dragging && Math.abs(dy) > slop && Math.abs(dy) >= Math.abs(dx)) vertical = true;
                    if (!dragging && !vertical && Math.abs(dx) > slop * 2 && Math.abs(dx) > Math.abs(dy) * 1.5f) {
                        dragging = true; MotionEvent cancel = MotionEvent.obtain(event); cancel.setAction(MotionEvent.ACTION_CANCEL); list.onTouchEvent(cancel); cancel.recycle(); list.getParent().requestDisallowInterceptTouchEvent(true);
                    }
                    if (dragging) { card.setTranslationX(dx); card.setAlpha(Math.max(0.55f, 1 - Math.abs(dx) / Math.max(1, card.getWidth()))); return true; }
                }
                if (type == MotionEvent.ACTION_UP || type == MotionEvent.ACTION_CANCEL) {
                    boolean consumed = dragging, remove = dragging && type == MotionEvent.ACTION_UP && Math.abs(dx) >= Math.max(dp(96), card.getWidth() * 0.3f);
                    Runnable selected = action; reset(); if (remove) selected.run(); return consumed;
                }
                return dragging;
            }
            private void reset() { if (card != null) { card.setTranslationX(0); card.setAlpha(1); } list.getParent().requestDisallowInterceptTouchEvent(false); card = null; action = null; dragging = vertical = false; }
        });
    }

    private void updateEpisodeStates() {
        episodeJobs.clear();
        for (DownloadStore.Job job : store.snapshot()) {
            DownloadStore.Job previous = episodeJobs.get(job.url);
            if (previous == null || DownloadStore.DONE.equals(previous.state) || !DownloadStore.DONE.equals(job.state)) episodeJobs.put(job.url, job);
        }
    }

    private void loadImage(String url, ImageView target) {
        if (!url.equals(target.getTag())) { target.setTag(url); target.setScaleType(ImageView.ScaleType.FIT_CENTER); target.setPadding(dp(12), dp(12), dp(12), dp(12)); target.setImageResource(R.drawable.ic_poster); }
        if (url.isEmpty()) return;
        Bitmap cached = images.get(url); if (cached != null) { target.setPadding(0, 0, 0, 0); target.setScaleType(ImageView.ScaleType.CENTER_CROP); target.setImageBitmap(cached); return; }
        if (imageFailures.contains(url) || !imageRequests.add(url)) return;
        network.execute(() -> {
            boolean loaded = false;
            try {
                byte[] data = SiteClient.read(url, SiteClient.CANONICAL + "/", 4 * 1024 * 1024, new SiteClient.Stop()).body();
                BitmapFactory.Options bounds = new BitmapFactory.Options(); bounds.inJustDecodeBounds = true; BitmapFactory.decodeByteArray(data, 0, data.length, bounds);
                if (bounds.outWidth <= 0 || bounds.outHeight <= 0 || (long) bounds.outWidth * bounds.outHeight > 100_000_000) return;
                BitmapFactory.Options options = new BitmapFactory.Options(); options.inSampleSize = Math.max(1, Math.max(bounds.outWidth, bounds.outHeight) / 240);
                Bitmap bitmap = BitmapFactory.decodeByteArray(data, 0, data.length, options);
                if (bitmap != null) { loaded = true; images.put(url, bitmap); handler.post(() -> { if (!destroyed) { if (url.equals(target.getTag())) { target.setPadding(0, 0, 0, 0); target.setScaleType(ImageView.ScaleType.CENTER_CROP); target.setImageBitmap(bitmap); } if (tab == 0 && contentUrl.isEmpty() && resultAdapter != null) resultAdapter.notifyDataSetChanged(); } }); }
            } catch (Exception ignored) { }
            finally { boolean failed = !loaded; handler.post(() -> { imageRequests.remove(url); if (failed) imageFailures.add(url); }); }
        });
    }

    private void showQueue() {
        LinearLayout layout = column(); layout.setPadding(dp(20), dp(8), dp(20), 0); content.addView(layout, new FrameLayout.LayoutParams(-1, -1));
        queueAdapter = new BaseAdapter() {
            public int getCount() { return jobs.size(); }
            public Object getItem(int position) { return jobs.get(position); }
            public long getItemId(int position) { return jobs.get(position).id.hashCode(); }
            public View getView(int position, View recycled, ViewGroup parent) { return jobRow(jobs.get(position), recycled); }
        };
        LinearLayout header = row(); header.setGravity(Gravity.CENTER_VERTICAL);
        TextView heading = text("다운로드 대기열", 24, INK, true); heading.setAccessibilityHeading(true); heading.setPadding(0, dp(4), dp(8), dp(4)); header.addView(heading, new LinearLayout.LayoutParams(0, -2, 1));
        queueClear = button("전체 삭제", false); queueClear.setSingleLine(true); queueClear.setMinHeight(dp(48)); queueClear.setMinimumHeight(dp(48)); queueClear.setTextColor(DANGER); queueClear.setContentDescription("다운로드 대기열 전체 삭제"); queueClear.setOnClickListener(v -> confirmRemoveJobs(store.snapshot(), true)); header.addView(queueClear); layout.addView(header);
        queueSummary = text("", 14, MUTED, false); queueSummary.setPadding(0, 0, 0, dp(12)); layout.addView(queueSummary);
        if (heading.getPaint().measureText("다운로드 대기열") + queueClear.getPaint().measureText("전체 삭제") + dp(32) > dp(getResources().getConfiguration().screenWidthDp - 40)) { header.setOrientation(LinearLayout.VERTICAL); heading.setLayoutParams(new LinearLayout.LayoutParams(-1, -2)); LinearLayout.LayoutParams clear = new LinearLayout.LayoutParams(-2, -2); clear.gravity = Gravity.END; clear.bottomMargin = dp(8); queueClear.setLayoutParams(clear); }
        ListView list = list(layout);
        list.setAdapter(queueAdapter); list.setOnItemClickListener((parent, view, position, id) -> showJobDetails((DownloadStore.Job) parent.getItemAtPosition(position)));
        list.setOnItemLongClickListener((parent, view, position, id) -> { Object item = parent.getItemAtPosition(position); if (!(item instanceof DownloadStore.Job job)) return false; jobActions(job); return true; });
        swipeToRemove(list, item -> item instanceof DownloadStore.Job job ? () -> { DownloadStore.Job current = store.find(job.id); if (current != null) { if (DownloadStore.DONE.equals(current.state)) removeJobs(java.util.Collections.singletonList(current.id)); else confirmRemoveJobs(java.util.Collections.singletonList(current), false); } } : null);
        queueActions = row(); queueActions.setPadding(0, dp(8), 0, dp(12));
        queuePause = button("전체 일시정지", false); queueStart = button("전체 시작", true);
        for (Button control : new Button[]{queuePause, queueStart}) { control.setSingleLine(true); control.setMinHeight(dp(48)); control.setMinimumHeight(dp(48)); }
        queueActions.addView(queuePause, weighted()); queueActions.addView(queueStart, weighted()); layout.addView(queueActions);
        queuePause.setOnClickListener(v -> pause()); queueStart.setOnClickListener(v -> { try { store.resume(null); startDownloads(); } catch (Exception e) { error(e); } }); updateQueue();
    }
    private void updateQueue() {
        if (queueAdapter == null) return;
        jobs = store.snapshot(); jobs.sort(java.util.Comparator.comparingInt(job -> DownloadStore.RUNNING.equals(job.state) ? 0 : DownloadStore.DONE.equals(job.state) ? 2 : 1)); queueAdapter.notifyDataSetChanged(); int done = 0, active = 0, waiting = 0, paused = 0, failed = 0;
        for (DownloadStore.Job job : jobs) { switch (job.state) { case DownloadStore.DONE -> done++; case DownloadStore.RUNNING -> active++; case DownloadStore.PAUSED -> paused++; case DownloadStore.FAILED -> failed++; default -> waiting++; } }
        String summary = "전체 " + jobs.size() + "건";
        if (active > 0) summary += " · 받는 중 " + active; if (waiting > 0) summary += " · 대기 " + waiting; if (paused > 0) summary += " · 일시정지 " + paused; if (failed > 0) summary += " · 실패 " + failed; if (done > 0) summary += " · 완료 " + done;
        boolean removing = removingJobs || TransferScheduler.removing(); queueClear.setVisibility(jobs.isEmpty() ? View.GONE : View.VISIBLE); queueClear.setEnabled(!controlsBusy());
        queueSummary.setText(removing ? "목록에서 제거하고 있습니다…" : jobs.isEmpty() ? "검색에서 회차를 골라 다운로드를 시작하세요." : (done == jobs.size() ? jobs.size() + "건 모두 다운로드 완료 · 재생할 영상을 고르세요." : summary) + "\n카드를 좌우로 밀어 목록에서 삭제할 수 있습니다.");
        boolean canPause = active + waiting > 0, canStart = paused + failed > 0 || active == 0 && waiting > 0;
        queueActions.setVisibility(active + waiting + paused + failed >= 2 ? View.VISIBLE : View.GONE);
        queuePause.setVisibility(canPause ? View.VISIBLE : View.GONE); queueStart.setVisibility(canStart ? View.VISIBLE : View.GONE);
        queueStart.setText(paused > 0 ? "전체 재개" : failed > 0 ? "전체 재시도" : "전체 시작"); queueStart.setEnabled(!controlsBusy() && canStart); queuePause.setEnabled(!controlsBusy() && canPause);
        int available = (content.getWidth() > 0 ? content.getWidth() : dp(getResources().getConfiguration().screenWidthDp)) - dp(40);
        boolean stacked = canPause && canStart && 2 * Math.max(queuePause.getPaint().measureText(queuePause.getText().toString()), queueStart.getPaint().measureText(queueStart.getText().toString())) + dp(64) > available;
        int orientation = stacked ? LinearLayout.VERTICAL : LinearLayout.HORIZONTAL;
        if (queueActions.getOrientation() != orientation) { queueActions.setOrientation(orientation); for (Button control : new Button[]{queuePause, queueStart}) { LinearLayout.LayoutParams params = stacked ? new LinearLayout.LayoutParams(-1, -2) : weighted(); if (stacked) params.setMargins(dp(4), dp(4), dp(4), 0); control.setLayoutParams(params); } }
    }
    private static final class JobViews {
        LinearLayout card, top, labels, transfer; TextView title, meta, state, size, metrics, message; ProgressBar progress; Button action; String id;
    }
    private View jobRow(DownloadStore.Job job, View recycled) {
        JobViews views;
        if (recycled != null && recycled.getTag() instanceof JobViews holder) views = holder;
        else {
            views = new JobViews(); views.card = column(); views.card.setPadding(dp(16), dp(12), dp(16), dp(12)); views.card.setBackground(selectableBackground(Color.WHITE, 0)); views.card.setTag(views);
            views.top = row(); views.top.setGravity(Gravity.TOP); views.labels = column();
            views.title = text("", 16, INK, true); views.title.setMaxLines(2); views.title.setEllipsize(TextUtils.TruncateAt.END); views.labels.addView(views.title);
            views.meta = text("", 14, MUTED, false); views.meta.setPadding(0, dp(4), 0, 0); views.labels.addView(views.meta); views.top.addView(views.labels, new LinearLayout.LayoutParams(0, -2, 1)); views.card.addView(views.top);
            views.action = button("", false); views.action.setFocusable(false); views.action.setSingleLine(true); views.action.setMinHeight(dp(48)); views.action.setMinimumHeight(dp(48));
            LinearLayout.LayoutParams action = new LinearLayout.LayoutParams(-2, -2); action.leftMargin = dp(8); action.gravity = Gravity.TOP; views.top.addView(views.action, action);
            views.state = text("", 14, BLUE, true); views.state.setPadding(0, dp(8), 0, dp(4)); views.card.addView(views.state);
            views.progress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal); views.progress.setProgressTintList(android.content.res.ColorStateList.valueOf(BLUE)); views.card.addView(views.progress, new LinearLayout.LayoutParams(-1, dp(4)));
            views.transfer = row(); views.transfer.setGravity(Gravity.CENTER_VERTICAL); views.transfer.setPadding(0, dp(8), 0, 0); views.card.addView(views.transfer);
            views.size = text("", 13, MUTED, false); views.size.setPadding(0, 0, dp(8), 0); views.size.setMaxLines(1); views.size.setEllipsize(TextUtils.TruncateAt.END); views.transfer.addView(views.size, new LinearLayout.LayoutParams(0, -2, 1));
            views.metrics = text("", 13, MUTED, false); views.metrics.setTextScaleX(0.95f); views.metrics.setMaxLines(1); views.metrics.setEllipsize(TextUtils.TruncateAt.END); views.metrics.setGravity(Gravity.END); views.transfer.addView(views.metrics, new LinearLayout.LayoutParams(-2, -2));
            views.message = text("", 14, DANGER, false); views.message.setPadding(0, dp(8), 0, 0); views.message.setMaxLines(3); views.message.setEllipsize(TextUtils.TruncateAt.END); views.card.addView(views.message);
            JobViews bound = views; views.action.setOnClickListener(v -> { if (controlsBusy()) return; DownloadStore.Job current = store.find(bound.id); if (current == null) return; try { if (DownloadStore.DONE.equals(current.state)) openVideo(current); else if (DownloadStore.RUNNING.equals(current.state)) pause(current.id); else { store.resume(current.id); startDownloads(); } } catch (Exception e) { error(e); } });
        }
        views.id = job.id; boolean complete = DownloadStore.DONE.equals(job.state), running = DownloadStore.RUNNING.equals(job.state), waiting = DownloadStore.WAITING.equals(job.state);
        int actionWidth = Math.max(dp(80), (int) Math.ceil(views.action.getPaint().measureText("일시정지")) + views.action.getPaddingLeft() + views.action.getPaddingRight() + dp(2));
        int actionHeight = Math.max(dp(48), (int) Math.ceil(views.action.getPaint().getFontSpacing()) + views.action.getPaddingTop() + views.action.getPaddingBottom());
        LinearLayout.LayoutParams actionParams = (LinearLayout.LayoutParams) views.action.getLayoutParams();
        if (actionParams.width != actionWidth || actionParams.height != actionHeight) { actionParams.width = actionWidth; actionParams.height = actionHeight; views.action.setLayoutParams(actionParams); }
        views.title.setText(job.title); views.meta.setText(waiting ? "대기 중" : (job.durationSeconds > 0 ? "재생시간 " + duration(job.durationSeconds) : "") + (complete ? (job.durationSeconds > 0 ? " · " : "") + bytes(job.bytes) : "")); views.meta.setVisibility(waiting || complete || job.durationSeconds > 0 ? View.VISIBLE : View.GONE);
        int cardWidth = views.card.getWidth() > 0 ? views.card.getWidth() : (content.getWidth() > 0 ? content.getWidth() : dp(getResources().getConfiguration().screenWidthDp)) - dp(40);
        views.metrics.setMaxWidth((int) ((cardWidth - views.card.getPaddingLeft() - views.card.getPaddingRight() - dp(8)) * 0.6));
        boolean metaBelow = !waiting && views.meta.getPaint().measureText(views.meta.getText().toString()) > cardWidth - views.card.getPaddingLeft() - views.card.getPaddingRight() - actionWidth - dp(8);
        LinearLayout metaParent = metaBelow ? views.card : views.labels;
        if (views.meta.getParent() != metaParent) { ((ViewGroup) views.meta.getParent()).removeView(views.meta); if (metaBelow) views.card.addView(views.meta, 1, new LinearLayout.LayoutParams(-1, -2)); else views.labels.addView(views.meta, new LinearLayout.LayoutParams(-1, -2)); }
        views.state.setText(complete ? "다운로드 완료" : jobStatus(job) + (job.total > 0 ? " · " + Math.round(job.done * 100.0 / job.total) + "%" : "")); views.state.setTextColor(stateColor(job)); views.state.setVisibility(waiting ? View.GONE : View.VISIBLE);
        views.progress.setVisibility(complete || waiting ? View.GONE : View.VISIBLE); views.progress.setIndeterminate(running && job.total == 0); views.progress.setMax(Math.max(1, job.total)); views.progress.setProgress(job.done);
        views.size.setText(transferSize(job).replace(" / 약 ", " / ")); views.size.setVisibility(complete || waiting ? View.GONE : View.VISIBLE);
        views.transfer.setVisibility(complete || waiting ? View.GONE : View.VISIBLE);
        double speed = job.speed(), remaining = remaining(job, speed); views.metrics.setText(job.total > 0 && job.done == job.total ? "영상 파일로 저장 중…" : (speed > 0 ? rate(speed) : "속도 측정 중…") + (remaining > 0 ? "  " + eta(remaining) + " 남음" : "")); views.metrics.setVisibility(running ? View.VISIBLE : View.GONE);
        views.message.setText(job.message); views.message.setVisibility(DownloadStore.FAILED.equals(job.state) ? View.VISIBLE : View.GONE);
        String action = complete ? "재생" : running ? "일시정지" : DownloadStore.FAILED.equals(job.state) ? "재시도" : DownloadStore.PAUSED.equals(job.state) ? "재개" : "시작";
        views.action.setText(action); views.action.setContentDescription(job.title + ", " + action); views.action.setEnabled(!controlsBusy());
        dismissAction(views.card, "목록에서 삭제", () -> { DownloadStore.Job current = store.find(job.id); if (current != null) confirmRemoveJobs(java.util.Collections.singletonList(current), false); });
        return views.card;
    }

    private static int stateColor(DownloadStore.Job job) { return DownloadStore.DONE.equals(job.state) ? SUCCESS : DownloadStore.FAILED.equals(job.state) ? DANGER : DownloadStore.RUNNING.equals(job.state) ? BLUE : MUTED; }
    private static double remaining(DownloadStore.Job job, double speed) { return speed > 0 && job.done > 0 ? (job.bytes / (double) job.done) * Math.max(0, job.total - job.done) / speed : 0; }
    private static String transferSize(DownloadStore.Job job) { return bytes(job.bytes) + (job.done > 0 && job.total > job.done ? " / 약 " + bytes(Math.round(job.bytes * (job.total / (double) job.done))) : ""); }
    private void openVideo(DownloadStore.Job job) { openVideo(Uri.parse(job.output)); }
    private void openVideo(Uri uri) { try { startActivity(new Intent(Intent.ACTION_VIEW).setDataAndType(uri, "video/mp2t").addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)); } catch (android.content.ActivityNotFoundException e) { error(new Exception("이 영상을 열 수 있는 앱이 없습니다. 공유 기능을 이용해 주세요.")); } }
    private void shareVideo(Uri uri) { Intent share = new Intent(Intent.ACTION_SEND).setType("video/mp2t").putExtra(Intent.EXTRA_STREAM, uri).addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION); share.setClipData(ClipData.newRawUri("영상", uri)); startActivity(Intent.createChooser(share, "영상 공유")); }

    private record SavedVideo(Uri uri, String name, long size, long added) {}
    private static final class SavedVideoViews { LinearLayout card; TextView title, meta; ImageButton play, more; SavedVideo video; }
    private void showLibrary() {
        LinearLayout layout = page("보관함", "다운로드/TVMON의 영상입니다.\n대기열에서 지워도 재생할 수 있습니다.");
        LinearLayout tools = row(); tools.setGravity(Gravity.CENTER_VERTICAL); tools.setPadding(0, 0, 0, dp(12));
        libraryStatus = text("영상 파일을 불러오고 있습니다…", 14, INK, false); libraryStatus.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE); tools.addView(libraryStatus, new LinearLayout.LayoutParams(0, -2, 1));
        libraryRefresh = button("새로고침", false); libraryRefresh.setSingleLine(true); libraryRefresh.setMinHeight(dp(48)); libraryRefresh.setMinimumHeight(dp(48)); libraryRefresh.setOnClickListener(v -> loadSavedVideos()); tools.addView(libraryRefresh); layout.addView(tools);
        libraryEmpty = text("저장된 영상이 없습니다.\n다운로드한 영상이 여기에 표시됩니다.", 16, MUTED, false); libraryEmpty.setPadding(0, dp(20), 0, dp(20)); libraryEmpty.setVisibility(View.GONE); layout.addView(libraryEmpty);
        ListView list = list(layout); libraryAdapter = new BaseAdapter() {
            public int getCount() { return savedVideos.size(); }
            public Object getItem(int position) { return savedVideos.get(position); }
            public long getItemId(int position) { return ContentUris.parseId(savedVideos.get(position).uri()); }
            public View getView(int position, View recycled, ViewGroup parent) {
                SavedVideoViews views;
                if (recycled != null && recycled.getTag() instanceof SavedVideoViews holder) views = holder;
                else {
                    views = new SavedVideoViews(); views.card = row(); views.card.setGravity(Gravity.CENTER_VERTICAL); views.card.setPadding(dp(16), dp(12), dp(12), dp(12)); views.card.setBackground(selectableBackground(Color.WHITE, 0)); views.card.setTag(views);
                    LinearLayout labels = column(); views.title = text("", 16, INK, true); views.title.setMaxLines(2); views.title.setEllipsize(TextUtils.TruncateAt.END); labels.addView(views.title); views.meta = text("", 14, MUTED, false); views.meta.setPadding(0, dp(4), 0, 0); labels.addView(views.meta); views.card.addView(labels, new LinearLayout.LayoutParams(0, -2, 1));
                    views.play = new ImageButton(MainActivity.this); views.play.setImageResource(R.drawable.ic_play); views.play.setImageTintList(android.content.res.ColorStateList.valueOf(BLUE)); views.play.setPadding(dp(12), dp(12), dp(12), dp(12)); views.play.setScaleType(ImageView.ScaleType.CENTER_INSIDE); views.play.setBackground(selectableBackground(SOFT_BLUE, 0)); LinearLayout.LayoutParams play = new LinearLayout.LayoutParams(dp(48), dp(48)); play.leftMargin = dp(8); views.card.addView(views.play, play);
                    views.more = new ImageButton(MainActivity.this); views.more.setImageResource(R.drawable.ic_more); views.more.setImageTintList(android.content.res.ColorStateList.valueOf(MUTED)); views.more.setPadding(dp(12), dp(12), dp(12), dp(12)); views.more.setScaleType(ImageView.ScaleType.CENTER_INSIDE); views.more.setBackground(selectableBackground(Color.WHITE, 0)); LinearLayout.LayoutParams more = new LinearLayout.LayoutParams(dp(48), dp(48)); more.leftMargin = dp(4); views.card.addView(views.more, more);
                    SavedVideoViews bound = views; views.card.setOnClickListener(v -> openVideo(bound.video.uri())); views.play.setOnClickListener(v -> openVideo(bound.video.uri())); views.more.setOnClickListener(v -> savedVideoActions(bound.video));
                }
                views.video = savedVideos.get(position); String title = savedVideoTitle(views.video); views.title.setText(title);
                String date = android.text.format.DateFormat.format("yyyy.MM.dd", new java.util.Date(views.video.added() * 1000)).toString(); int dot = views.video.name().lastIndexOf('.');
                String format = ambiguousSavedTitles.contains(savedVideoStem(views.video.name()).toLowerCase(Locale.ROOT)) && dot > 0 && dot < views.video.name().length() - 1 ? views.video.name().substring(dot + 1).toUpperCase(Locale.ROOT) + " · " : ""; views.meta.setText(format + bytes(views.video.size()) + " · " + date);
                views.card.setContentDescription(title + ", " + views.meta.getText() + ", 영상 재생"); views.play.setContentDescription(title + ", 영상 재생"); views.more.setContentDescription(title + ", 더보기"); return views.card;
            }
        }; list.setAdapter(libraryAdapter); list.setOnItemClickListener((parent, view, position, id) -> { Object item = parent.getItemAtPosition(position); if (item instanceof SavedVideo video) openVideo(video.uri()); }); loadSavedVideos();
    }
    private void loadSavedVideos() {
        int revision = ++libraryRevision; libraryRefresh.setEnabled(false); libraryStatus.setText("영상 파일을 불러오고 있습니다…");
        network.execute(() -> {
            List<SavedVideo> found = new ArrayList<>();
            try (Cursor cursor = getContentResolver().query(MediaStore.Downloads.EXTERNAL_CONTENT_URI, new String[]{MediaStore.Downloads._ID, MediaStore.Downloads.DISPLAY_NAME, MediaStore.Downloads.SIZE, MediaStore.Downloads.DATE_ADDED}, MediaStore.Downloads.RELATIVE_PATH + "=? AND " + MediaStore.Downloads.IS_PENDING + "=0 AND " + MediaStore.Downloads.SIZE + ">0", new String[]{android.os.Environment.DIRECTORY_DOWNLOADS + "/TVMON/"}, MediaStore.Downloads.DATE_ADDED + " DESC, " + MediaStore.Downloads._ID + " DESC")) {
                if (cursor == null) throw new java.io.IOException("저장된 영상 목록을 읽지 못했습니다.");
                while (cursor.moveToNext()) found.add(new SavedVideo(ContentUris.withAppendedId(MediaStore.Downloads.EXTERNAL_CONTENT_URI, cursor.getLong(0)), cursor.getString(1), cursor.getLong(2), cursor.getLong(3)));
                handler.post(() -> {
                    if (destroyed || tab != 3 || libraryRevision != revision) return;
                    if (!savedVideos.equals(found)) {
                        savedVideos.clear(); savedVideos.addAll(found); ambiguousSavedTitles.clear(); java.util.Set<String> seen = new java.util.HashSet<>();
                        for (SavedVideo video : savedVideos) { String stem = savedVideoStem(video.name()).toLowerCase(Locale.ROOT); if (!seen.add(stem)) ambiguousSavedTitles.add(stem); }
                        libraryAdapter.notifyDataSetChanged();
                    }
                    libraryRefresh.setEnabled(true); libraryEmpty.setVisibility(found.isEmpty() ? View.VISIBLE : View.GONE);
                    long size = 0; for (SavedVideo video : savedVideos) size += video.size(); libraryStatus.setText("영상 " + found.size() + "개 · " + bytes(size));
                });
            } catch (Exception e) { handler.post(() -> { if (!destroyed && tab == 3 && libraryRevision == revision) { libraryRefresh.setEnabled(true); libraryEmpty.setVisibility(View.GONE); libraryStatus.setText("영상 목록을 읽지 못했습니다. 새로고침해 주세요.\n" + DownloadEngine.message(e)); } }); }
        });
    }
    private static String savedVideoStem(String name) { int dot = name.lastIndexOf('.'); return dot > 0 ? name.substring(0, dot) : name; }
    private String savedVideoTitle(SavedVideo video) { String stem = savedVideoStem(video.name()); return video.name().toLowerCase(Locale.ROOT).endsWith(".ts") && !ambiguousSavedTitles.contains(stem.toLowerCase(Locale.ROOT)) ? stem : video.name(); }
    private void savedVideoActions(SavedVideo video) {
        if (libraryMenuDialog != null) libraryMenuDialog.dismiss();
        Dialog dialog = new Dialog(this); libraryMenuDialog = dialog; dialog.requestWindowFeature(Window.FEATURE_NO_TITLE);
        ScrollView scroll = new ScrollView(this); LinearLayout sheet = column(); sheet.setPadding(dp(20), dp(8), dp(20), dp(12)); scroll.addView(sheet);
        GradientDrawable shape = background(Color.WHITE, 0); float radius = dp(20); shape.setCornerRadii(new float[]{radius, radius, radius, radius, 0, 0, 0, 0}); scroll.setBackground(shape);
        View handle = new View(this); handle.setBackground(background(0xffc6d0dc, 2)); LinearLayout.LayoutParams handleParams = new LinearLayout.LayoutParams(dp(32), dp(4)); handleParams.gravity = Gravity.CENTER_HORIZONTAL; handleParams.bottomMargin = dp(8); sheet.addView(handle, handleParams);
        LinearLayout header = row(); header.setGravity(Gravity.CENTER_VERTICAL); TextView heading = text("영상 메뉴", 20, INK, true); heading.setAccessibilityHeading(true); header.addView(heading, new LinearLayout.LayoutParams(0, -2, 1));
        ImageButton close = new ImageButton(this); close.setImageResource(android.R.drawable.ic_menu_close_clear_cancel); close.setImageTintList(android.content.res.ColorStateList.valueOf(MUTED)); close.setBackground(selectableBackground(Color.WHITE, 0)); close.setContentDescription("영상 메뉴 닫기"); header.addView(close, new LinearLayout.LayoutParams(dp(48), dp(48))); close.setOnClickListener(v -> dialog.dismiss()); sheet.addView(header);
        TextView title = text(video.name(), 16, INK, true); title.setMaxLines(2); title.setEllipsize(TextUtils.TruncateAt.END); title.setPadding(0, dp(8), 0, dp(16)); sheet.addView(title);
        Button play = button("영상 재생", false); play.setGravity(Gravity.START | Gravity.CENTER_VERTICAL); play.setCompoundDrawablesWithIntrinsicBounds(R.drawable.ic_play, 0, 0, 0); play.setCompoundDrawableTintList(android.content.res.ColorStateList.valueOf(BLUE)); play.setCompoundDrawablePadding(dp(12)); sheet.addView(play, new LinearLayout.LayoutParams(-1, -2)); play.setOnClickListener(v -> { dialog.dismiss(); openVideo(video.uri()); });
        Button delete = button("삭제", false); delete.setGravity(Gravity.START | Gravity.CENTER_VERTICAL); delete.setTextColor(DANGER); delete.setBackground(selectableBackground(Color.WHITE, 0)); delete.setCompoundDrawablesWithIntrinsicBounds(R.drawable.ic_delete, 0, 0, 0); delete.setCompoundDrawableTintList(android.content.res.ColorStateList.valueOf(DANGER)); delete.setCompoundDrawablePadding(dp(12)); LinearLayout.LayoutParams deleteParams = new LinearLayout.LayoutParams(-1, -2); deleteParams.topMargin = dp(8); sheet.addView(delete, deleteParams); delete.setOnClickListener(v -> { dialog.dismiss(); deleteSavedVideo(video); });
        dialog.setContentView(scroll); dialog.setOnDismissListener(d -> { if (libraryMenuDialog == dialog) libraryMenuDialog = null; }); Window window = dialog.getWindow(); window.setBackgroundDrawableResource(android.R.color.transparent); window.addFlags(WindowManager.LayoutParams.FLAG_DIM_BEHIND); window.setDimAmount(0.40f); window.setGravity(Gravity.BOTTOM); dialog.show();
        View root = (View) content.getParent(); int available = root.getHeight() - root.getPaddingTop() - root.getPaddingBottom(); scroll.measure(View.MeasureSpec.makeMeasureSpec(root.getWidth(), View.MeasureSpec.EXACTLY), View.MeasureSpec.makeMeasureSpec(0, View.MeasureSpec.UNSPECIFIED)); window.setLayout(-1, Math.min(scroll.getMeasuredHeight(), (int) (available * 0.85)));
    }
    private void deleteSavedVideo(SavedVideo video) {
        confirmRemoval("영상 파일을 삭제할까요?", video.name() + "\n삭제한 파일은 복구할 수 없습니다.", "파일 삭제", () -> new Thread(() -> {
            try {
                if (getContentResolver().delete(video.uri(), null, null) == 0) throw new java.io.IOException("영상 파일을 삭제하지 못했습니다. 새로고침한 뒤 다시 시도해 주세요.");
                for (DownloadStore.Job job : store.snapshot()) if (DownloadStore.DONE.equals(job.state) && video.uri().toString().equals(job.output)) store.remove(job.id);
                handler.post(() -> { if (destroyed) return; if (tab == 3) loadSavedVideos(); else if (tab == 1) updateQueue(); Toast.makeText(this, "영상 파일을 삭제했습니다.", Toast.LENGTH_SHORT).show(); });
            } catch (Exception e) { android.util.Log.e("TVMON", "영상 파일 삭제 실패", e); handler.post(() -> { if (!destroyed) error(e); }); }
        }, "tvmon-delete-file").start());
    }

    private static String jobStatus(DownloadStore.Job job) {
        return switch (job.state) { case DownloadStore.RUNNING -> "다운로드 중"; case DownloadStore.PAUSED -> "일시정지"; case DownloadStore.DONE -> "완료"; case DownloadStore.FAILED -> "다운로드 실패"; default -> "대기 중"; };
    }

    private void showJobDetails(DownloadStore.Job job) {
        if (detailsDialog != null) detailsDialog.dismiss();
        detailsId = job.id; detailsMetadataFailed = detailsWorkersExpanded = detailsMenuShowing = false; detailsPartsExpanded = !DownloadStore.DONE.equals(job.state); detailsLayoutMode = ""; detailsStop = new SiteClient.Stop();
        detailsScrollPosition = 0;
        Dialog dialog = new Dialog(this) {
            // Android 10–12 back key; Android 13+ use the dispatcher registered below.
            @android.annotation.SuppressLint("GestureBackNavigation")
            @Override public void onBackPressed() { if (detailsMenuShowing) showDetailsMenu(false); else super.onBackPressed(); }
            @Override public void onWindowFocusChanged(boolean focused) {
                super.onWindowFocusChanged(focused);
                if (detailsDialog != this || detailsScroll == null) return;
                if (!focused) detailsScrollPosition = detailsScroll.getScrollY();
                else { int position = detailsScrollPosition; detailsScroll.requestFocus(); detailsScroll.post(() -> detailsScroll.scrollTo(0, position)); }
            }
        }; detailsDialog = dialog; dialog.requestWindowFeature(Window.FEATURE_NO_TITLE);
        LinearLayout sheet = column(); sheet.setPadding(dp(20), dp(8), dp(20), dp(12));
        GradientDrawable shape = background(Color.WHITE, 0); float radius = dp(20); shape.setCornerRadii(new float[]{radius, radius, radius, radius, 0, 0, 0, 0}); sheet.setBackground(shape);
        View handle = new View(this); handle.setBackground(background(0xffc6d0dc, 2)); LinearLayout.LayoutParams handleParams = new LinearLayout.LayoutParams(dp(32), dp(4)); handleParams.gravity = Gravity.CENTER_HORIZONTAL; handleParams.bottomMargin = dp(8); sheet.addView(handle, handleParams);
        LinearLayout header = row(); header.setGravity(Gravity.CENTER_VERTICAL);
        detailsHeading = text("다운로드 상세", 20, INK, true); detailsHeading.setAccessibilityHeading(true); header.addView(detailsHeading, new LinearLayout.LayoutParams(0, -2, 1));
        Button menu = button("메뉴", false); detailsMenuButton = menu; menu.setSingleLine(true); menu.setMinWidth(dp(56)); header.addView(menu, new LinearLayout.LayoutParams(-2, -2));
        menu.setOnClickListener(v -> { if (detailsMenuShowing) showDetailsMenu(false); else { DownloadStore.Job current = store.find(detailsId); if (current != null) jobActions(current); } });
        ImageButton close = new ImageButton(this); close.setImageResource(android.R.drawable.ic_menu_close_clear_cancel); close.setImageTintList(android.content.res.ColorStateList.valueOf(MUTED)); close.setContentDescription("다운로드 상세 닫기"); close.setBackgroundResource(android.R.drawable.list_selector_background); header.addView(close, new LinearLayout.LayoutParams(dp(48), dp(48))); close.setOnClickListener(v -> dialog.dismiss()); sheet.addView(header);
        ScrollView scroll = new ScrollView(this); detailsScroll = scroll; scroll.setFillViewport(false); scroll.setClipToPadding(true); scroll.setFocusableInTouchMode(true); LinearLayout scrollBody = column(); scroll.addView(scrollBody); sheet.addView(scroll, new LinearLayout.LayoutParams(-1, 0, 1));
        LinearLayout body = column(); detailsContent = body; body.setPadding(0, dp(12), 0, dp(12)); scrollBody.addView(body); detailsMenu = column(); detailsMenu.setPadding(0, 0, 0, dp(8)); detailsMenu.setVisibility(View.GONE); scrollBody.addView(detailsMenu);
        detailsTitle = text(job.title, 20, INK, true); body.addView(detailsTitle);
        detailsDuration = text("", 14, MUTED, false); detailsDuration.setPadding(0, dp(2), 0, dp(4)); body.addView(detailsDuration);
        LinearLayout state = row(); state.setGravity(Gravity.CENTER_VERTICAL); detailsState = text("", 14, BLUE, true); state.addView(detailsState, new LinearLayout.LayoutParams(0, -2, 1)); detailsPercent = text("", 28, BLUE, true); state.addView(detailsPercent); body.addView(state);
        ProgressBar progress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal); progress.setMax(1000); progress.setProgressTintList(android.content.res.ColorStateList.valueOf(BLUE)); progress.setTag("details-progress"); body.addView(progress, new LinearLayout.LayoutParams(-1, dp(6)));
        LinearLayout metrics = row(); detailsMetrics = metrics; metrics.setPadding(0, dp(12), 0, dp(12)); LinearLayout speed = column(), eta = column(); speed.addView(text("현재 속도", 14, MUTED, false)); eta.addView(text("남은 시간 · 예상", 14, MUTED, false));
        detailsSpeed = text("", 24, BLUE, true); detailsEta = text("", 22, BLUE, true); detailsSpeed.setPadding(0, dp(4), 0, 0); detailsEta.setPadding(0, dp(4), 0, 0); speed.addView(detailsSpeed); eta.addView(detailsEta);
        metrics.addView(speed, new LinearLayout.LayoutParams(0, -2, 1)); metrics.addView(eta, new LinearLayout.LayoutParams(0, -2, 1)); body.addView(metrics);
        detailsSaved = text("", 16, INK, false); detailsSaved.setPadding(0, dp(8), 0, dp(12)); body.addView(detailsSaved);
        detailsPartsButton = button("조각 상세", false); detailsPartsButton.setGravity(Gravity.START | Gravity.CENTER_VERTICAL); body.addView(detailsPartsButton, new LinearLayout.LayoutParams(-1, -2));
        detailsPartsButton.setOnClickListener(v -> { detailsPartsExpanded = !detailsPartsExpanded; updateDetails(); });
        detailsParts = column(); body.addView(detailsParts);
        detailsCount = text("", 14, MUTED, false); detailsCount.setPadding(0, dp(12), 0, 0); detailsParts.addView(detailsCount);
        detailsMap = new SegmentMap(); LinearLayout.LayoutParams mapParams = new LinearLayout.LayoutParams(-1, -2); mapParams.topMargin = dp(12); detailsParts.addView(detailsMap, mapParams);
        detailsGrouping = text("", 14, MUTED, false); detailsGrouping.setPadding(0, dp(8), 0, dp(8)); detailsParts.addView(detailsGrouping);
        LinearLayout legend = row(); legend.addView(legend("저장 완료", 0xff3d9b72), new LinearLayout.LayoutParams(0, -2, 1)); legend.addView(legend("받는 중", 0xff2874d0), new LinearLayout.LayoutParams(0, -2, 1)); legend.addView(legend("대기", 0xffe2e8f0), new LinearLayout.LayoutParams(0, -2, 1)); detailsParts.addView(legend);
        TextView retry = legend("오류 · 재시도", 0xffd97730); retry.setTag("details-retry"); retry.setVisibility(View.GONE); retry.setPadding(0, dp(8), 0, 0); detailsParts.addView(retry);
        detailsWorkersButton = button("", false); detailsWorkersButton.setGravity(Gravity.CENTER_VERTICAL | Gravity.START); LinearLayout.LayoutParams workersParams = new LinearLayout.LayoutParams(-1, dp(48)); workersParams.topMargin = dp(8); body.addView(detailsWorkersButton, workersParams);
        detailsWorkersButton.setCompoundDrawablesWithIntrinsicBounds(0, 0, android.R.drawable.arrow_down_float, 0);
        detailsWorkers = text("", 14, MUTED, false); detailsWorkers.setPadding(dp(12), dp(8), dp(12), dp(8)); detailsWorkers.setVisibility(View.GONE); body.addView(detailsWorkers);
        detailsWorkersButton.setOnClickListener(v -> { detailsWorkersExpanded = !detailsWorkersExpanded; detailsWorkers.setVisibility(detailsWorkersExpanded ? View.VISIBLE : View.GONE); detailsWorkersButton.setCompoundDrawablesWithIntrinsicBounds(0, 0, detailsWorkersExpanded ? android.R.drawable.arrow_up_float : android.R.drawable.arrow_down_float, 0); updateDetails(); });
        detailsMessage = text("", 13, MUTED, false); detailsMessage.setPadding(0, dp(10), 0, 0); body.addView(detailsMessage);
        detailsAction = button("", true); sheet.addView(detailsAction, new LinearLayout.LayoutParams(-1, -2));
        detailsAction.setOnClickListener(v -> {
            DownloadStore.Job current = store.find(detailsId); if (current == null) return;
            try {
                if (DownloadStore.RUNNING.equals(current.state) || DownloadStore.WAITING.equals(current.state)) pause(current.id);
                else if (DownloadStore.DONE.equals(current.state)) openVideo(current);
                else { store.resume(current.id); startDownloads(); }
                updateDetails();
            } catch (android.content.ActivityNotFoundException e) { error(new Exception("이 영상을 열 수 있는 앱이 없습니다. 메뉴의 공유 기능을 이용해 주세요.")); }
            catch (Exception e) { error(e); }
        });
        dialog.setContentView(sheet); dialog.setOnDismissListener(d -> { if (detailsDialog == dialog) { detailsDialog = null; detailsId = null; detailsStop.cancel(); handler.removeCallbacks(refreshDetails); } });
        Window window = dialog.getWindow(); window.setBackgroundDrawableResource(android.R.color.transparent); window.addFlags(WindowManager.LayoutParams.FLAG_DIM_BEHIND); window.setDimAmount(0.40f); window.setGravity(Gravity.BOTTOM);
        dialog.show(); if (Build.VERSION.SDK_INT >= 33) dialog.getOnBackInvokedDispatcher().registerOnBackInvokedCallback(OnBackInvokedDispatcher.PRIORITY_DEFAULT, () -> { if (detailsMenuShowing) showDetailsMenu(false); else dialog.dismiss(); });
        View root = (View) content.getParent(); int available = root.getHeight() - root.getPaddingTop() - root.getPaddingBottom(); window.setLayout(-1, (int) (available * 0.86));
        scroll.requestFocus(); scroll.post(() -> scroll.scrollTo(0, 0));
        updateDetails(); handler.postDelayed(refreshDetails, 500);
        SiteClient.Stop stop = detailsStop;
        network.execute(() -> {
            try {
                store.restorePartStates(job.id);
                if (job.durationSeconds <= 0) {
                    SiteClient client = client(); SiteClient.Video video = client.video(job.url, stop);
                    DownloadEngine.Source source = DownloadEngine.loadSource(client, video.manifest(), stop);
                    double seconds = 0; for (VideoFormat.Segment segment : source.segments()) seconds += segment.duration(); double duration = seconds;
                    stop.check(); store.update(job.id, current -> current.durationSeconds = duration);
                }
            } catch (Exception e) { handler.post(() -> { if (detailsStop == stop && !stop.stopped()) detailsMetadataFailed = true; }); }
            handler.post(() -> { if (detailsStop == stop && !stop.stopped()) updateDetails(); });
        });
    }

    private void updateDetails() {
        if (detailsDialog == null || !detailsDialog.isShowing()) return;
        DownloadStore.Job job = store.find(detailsId); if (job == null) { detailsDialog.dismiss(); return; }
        detailsTitle.setText(job.title); detailsDuration.setText(job.durationSeconds > 0 ? "재생시간 " + duration(job.durationSeconds) : detailsMetadataFailed ? "재생시간을 확인하지 못했습니다." : "재생시간 확인 중…");
        boolean complete = DownloadStore.DONE.equals(job.state), running = DownloadStore.RUNNING.equals(job.state);
        if (complete && !detailsLayoutMode.isEmpty() && !detailsLayoutMode.startsWith(DownloadStore.DONE + "/")) detailsPartsExpanded = false;
        detailsState.setText(complete ? "다운로드 완료" : jobStatus(job)); detailsState.setTextColor(stateColor(job));
        detailsPercent.setVisibility(complete ? View.GONE : View.VISIBLE);
        detailsPercent.setText(job.total > 0 ? String.format(Locale.KOREA, "%.0f%%", job.done * 100.0 / job.total) : "—");
        ProgressBar progress = detailsDialog.findViewById(android.R.id.progress); if (progress == null) progress = detailsDialog.getWindow().getDecorView().findViewWithTag("details-progress"); progress.setProgress(job.total > 0 ? (int) (job.done * 1000L / job.total) : 0);
        progress.setVisibility(complete ? View.GONE : View.VISIBLE); detailsMetrics.setVisibility(running ? View.VISIBLE : View.GONE);
        detailsSaved.setText(complete ? "저장한 용량 " + bytes(job.bytes) : "받은 용량 " + transferSize(job)); double speed = job.speed();
        detailsSpeed.setText(speed > 0 ? rate(speed) : DownloadStore.RUNNING.equals(job.state) ? "측정 중…" : "—");
        double remaining = remaining(job, speed);
        detailsEta.setText(DownloadStore.DONE.equals(job.state) ? "완료" : job.total > 0 && job.done == job.total ? "병합 중" : remaining > 0 ? "약 " + eta(remaining) : "—");
        detailsCount.setText(job.total > 0 ? String.format(Locale.KOREA, "%,d / %,d 조각", job.done, job.total) : "목록 확인 중");
        detailsPartsButton.setText(job.total > 0 ? String.format(Locale.KOREA, "조각 %,d / %,d개%s", job.done, job.total, complete ? " 완료" : "") : "조각 상세 · 목록 확인 중");
        detailsPartsButton.setCompoundDrawablesWithIntrinsicBounds(0, 0, detailsPartsExpanded ? android.R.drawable.arrow_up_float : android.R.drawable.arrow_down_float, 0); disclosureState(detailsPartsButton, detailsPartsExpanded);
        detailsParts.setVisibility(detailsPartsExpanded ? View.VISIBLE : View.GONE);
        if (detailsPartsExpanded) detailsMap.update(job);
        detailsGrouping.setText(job.total == 0 ? "영상 목록을 확인하면 조각이 표시됩니다." : job.partStates == null ? "저장한 조각 기록을 확인하고 있습니다…" : "한 칸 = 최대 " + detailsMap.group + "개 조각");
        int active = 0; boolean failed = false; StringBuilder workers = new StringBuilder();
        if (job.partStates != null) for (byte state : job.partStates) if (state == 3) failed = true;
        detailsDialog.getWindow().getDecorView().findViewWithTag("details-retry").setVisibility(failed ? View.VISIBLE : View.GONE);
        if (job.partStates != null) for (int i = 0; i < job.partStates.length; i++) if (job.partStates[i] == 1 && DownloadStore.RUNNING.equals(job.state)) { active++; if (workers.length() > 0) workers.append('\n'); workers.append("작업 ").append(active).append(" · ").append(i + 1).append("번 조각 수신 중"); }
        detailsWorkersButton.setText("진행 중인 작업 " + active + "개"); detailsWorkers.setText(active == 0 ? "현재 수신 중인 조각이 없습니다." : workers.toString());
        detailsWorkersButton.setVisibility(running ? View.VISIBLE : View.GONE); detailsWorkers.setVisibility(running && detailsWorkersExpanded ? View.VISIBLE : View.GONE); disclosureState(detailsWorkersButton, detailsWorkersExpanded);
        detailsMessage.setText(job.message); detailsMessage.setVisibility(DownloadStore.FAILED.equals(job.state) || DownloadStore.RUNNING.equals(job.state) && (job.total == 0 || job.done == job.total) ? View.VISIBLE : View.GONE);
        detailsAction.setText(complete ? "영상 열기" : running || DownloadStore.WAITING.equals(job.state) ? "일시정지" : "이어받기 / 시작");
        detailsAction.setEnabled(!controlsBusy());
        String mode = job.state + "/" + detailsPartsExpanded + "/" + detailsWorkersExpanded + "/" + detailsMenuShowing + "/" + (job.durationSeconds > 0);
        if (!mode.equals(detailsLayoutMode)) { detailsLayoutMode = mode; detailsScroll.post(this::resizeDetails); }
    }

    private void disclosureState(Button button, boolean expanded) { if (Build.VERSION.SDK_INT >= 30) button.setStateDescription(expanded ? "펼쳐짐" : "접힘"); else button.setContentDescription(button.getText() + (expanded ? ", 펼쳐짐" : ", 접힘")); }
    private void resizeDetails() {
        if (detailsDialog == null || !detailsDialog.isShowing()) return;
        LinearLayout sheet = (LinearLayout) detailsScroll.getParent(); View root = (View) content.getParent(); int available = root.getHeight() - root.getPaddingTop() - root.getPaddingBottom();
        int width = Math.max(dp(120), root.getWidth() - sheet.getPaddingLeft() - sheet.getPaddingRight()); View body = detailsScroll.getChildAt(0);
        body.measure(View.MeasureSpec.makeMeasureSpec(width, View.MeasureSpec.EXACTLY), View.MeasureSpec.makeMeasureSpec(0, View.MeasureSpec.UNSPECIFIED));
        int fixed = sheet.getPaddingTop() + sheet.getPaddingBottom(); for (int i = 0; i < sheet.getChildCount(); i++) { View child = sheet.getChildAt(i); if (child == detailsScroll || child.getVisibility() == View.GONE) continue; child.measure(View.MeasureSpec.makeMeasureSpec(width, View.MeasureSpec.AT_MOST), View.MeasureSpec.makeMeasureSpec(0, View.MeasureSpec.UNSPECIFIED)); LinearLayout.LayoutParams p = (LinearLayout.LayoutParams) child.getLayoutParams(); fixed += child.getMeasuredHeight() + p.topMargin + p.bottomMargin; }
        int height = Math.min((int) (available * 0.90), fixed + body.getMeasuredHeight()); detailsDialog.getWindow().setLayout(-1, height);
    }

    private void divider(LinearLayout layout) { View line = new View(this); line.setBackgroundColor(0xffe0e6ee); LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, dp(1)); params.setMargins(0, dp(8), 0, dp(8)); layout.addView(line, params); }
    private TextView legend(String label, int color) { TextView view = text(label, 14, MUTED, false); GradientDrawable swatch = background(color, 1); swatch.setBounds(0, 0, dp(12), dp(12)); view.setCompoundDrawables(swatch, null, null, null); view.setCompoundDrawablePadding(dp(4)); return view; }
    private static String duration(double seconds) { long value = Math.round(seconds); return String.format(Locale.KOREA, "%02d:%02d:%02d", value / 3600, value / 60 % 60, value % 60); }
    private static String eta(double seconds) { long value = Math.max(1, (long) Math.ceil(seconds)); return value >= 3600 ? value / 3600 + "시간 " + value / 60 % 60 + "분" : value >= 60 ? value / 60 + "분 " + value % 60 + "초" : value + "초"; }
    private static String rate(double value) { return value >= 1024 * 1024 ? String.format(Locale.KOREA, "%.1f MB/s", value / (1024 * 1024)) : value >= 1024 ? String.format(Locale.KOREA, "%.0f KB/s", value / 1024) : String.format(Locale.KOREA, "%.0f B/s", value); }

    private final class SegmentMap extends View {
        private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private byte[] states;
        private int total, group = 1;
        SegmentMap() { super(MainActivity.this); setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_YES); }
        void update(DownloadStore.Job job) {
            boolean resized = total != job.total; total = job.total; states = job.partStates; group = Math.max(1, (total + 199) / 200);
            setContentDescription("전체 " + total + "개 조각 중 " + job.done + "개 저장 완료"); if (resized) requestLayout(); invalidate();
        }
        @Override protected void onMeasure(int widthSpec, int heightSpec) { int width = MeasureSpec.getSize(widthSpec); if (total == 0) { setMeasuredDimension(width, dp(24)); return; } int cells = Math.max(1, (total + group - 1) / group), columns = Math.min(20, cells); float tile = (width - dp(2) * (columns - 1)) / (float) columns; setMeasuredDimension(width, Math.round((Math.min(dp(12), tile * 0.6f) + dp(2)) * ((cells + columns - 1) / columns) - dp(2))); }
        @Override protected void onDraw(Canvas canvas) {
            super.onDraw(canvas); if (total == 0) return;
            int cells = (total + group - 1) / group, columns = Math.min(20, cells); float gap = dp(2), tile = (getWidth() - gap * (columns - 1)) / columns, tileHeight = Math.min(dp(12), tile * 0.6f);
            for (int cell = 0; cell < cells; cell++) {
                int first = cell * group, last = Math.min(total, first + group), saved = 0; boolean receiving = false, failed = false;
                if (states != null) for (int i = first; i < last && i < states.length; i++) { if (states[i] == 2) saved++; if (states[i] == 1) receiving = true; if (states[i] == 3) failed = true; }
                float left = (cell % columns) * (tile + gap), top = (cell / columns) * (tileHeight + gap); paint.setColor(0xffe2e8f0); canvas.drawRect(left, top, left + tile, top + tileHeight, paint);
                if (saved > 0) { paint.setColor(0xff3d9b72); canvas.drawRect(left, top, left + tile * saved / (last - first), top + tileHeight, paint); }
                if (receiving || failed) { paint.setColor(receiving ? 0xff2874d0 : 0xffd97730); canvas.drawRect(left, top, left + tile, top + tileHeight, paint); }
            }
        }
    }

    private void jobActions(DownloadStore.Job job) {
        if (detailsDialog == null || !job.id.equals(detailsId)) showJobDetails(job);
        detailsMenu.removeAllViews(); TextView title = text(job.title, 18, INK, true); title.setPadding(0, dp(16), 0, dp(12)); detailsMenu.addView(title);
        if (DownloadStore.DONE.equals(job.state)) { menuAction("영상 열기", R.drawable.ic_play, false, () -> openVideo(job)); menuAction("공유", R.drawable.ic_share, false, () -> shareVideo(Uri.parse(job.output))); }
        else if (DownloadStore.RUNNING.equals(job.state)) menuAction("일시정지", R.drawable.ic_pause, false, () -> pause(job.id));
        else menuAction("이어받기 / 시작", R.drawable.ic_play, false, () -> { try { store.resume(job.id); startDownloads(); } catch (Exception e) { error(e); } });
        menuAction("영상 주소 복사", R.drawable.ic_copy, false, () -> { ((ClipboardManager) getSystemService(CLIPBOARD_SERVICE)).setPrimaryClip(ClipData.newPlainText("영상 주소", job.url)); Toast.makeText(this, "주소를 복사했습니다.", Toast.LENGTH_SHORT).show(); });
        divider(detailsMenu); menuAction("목록에서 삭제", R.drawable.ic_delete, true, () -> { DownloadStore.Job current = store.find(job.id); if (current != null) confirmRemoveJobs(java.util.Collections.singletonList(current), false); });
        showDetailsMenu(true);
    }
    private void menuAction(String label, int icon, boolean destructive, Runnable action) {
        Button item = button(label, false); item.setGravity(Gravity.START | Gravity.CENTER_VERTICAL); item.setBackground(selectableBackground(Color.WHITE, 0)); item.setTextColor(destructive ? DANGER : INK); item.setPadding(dp(12), dp(12), dp(12), dp(12));
        android.graphics.drawable.Drawable drawable = getDrawable(icon).mutate(); drawable.setTint(destructive ? DANGER : MUTED); drawable.setBounds(0, 0, dp(24), dp(24)); item.setCompoundDrawables(drawable, null, null, null); item.setCompoundDrawablePadding(dp(16)); detailsMenu.addView(item, new LinearLayout.LayoutParams(-1, -2));
        item.setOnClickListener(v -> {
            if (controlsBusy()) return;
            showDetailsMenu(false);
            try {
                action.run(); updateDetails();
            } catch (android.content.ActivityNotFoundException e) { error(new Exception("이 영상을 열 수 있는 앱이 없습니다. 공유 기능이나 파일 앱을 이용해 주세요.")); }
            catch (Exception e) { error(e); }
        });
    }
    private void showDetailsMenu(boolean show) {
        if (detailsDialog == null) return;
        if (show && !detailsMenuShowing) detailsScrollPosition = detailsScroll.getScrollY();
        detailsMenuShowing = show; detailsContent.setVisibility(show ? View.GONE : View.VISIBLE); detailsMenu.setVisibility(show ? View.VISIBLE : View.GONE); detailsAction.setVisibility(show ? View.GONE : View.VISIBLE);
        detailsHeading.setText(show ? "작업 메뉴" : "다운로드 상세"); detailsMenuButton.setText(show ? "상세" : "메뉴");
        int position = show ? 0 : detailsScrollPosition; detailsScroll.requestFocus(); detailsScroll.post(() -> detailsScroll.scrollTo(0, position)); updateDetails();
    }
    private void startDownloads() {
        if (controlsBusy()) return;
        try {
            if (store.next() == null) { Toast.makeText(this, "시작할 작업이 없습니다.", Toast.LENGTH_SHORT).show(); return; }
            if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1);
            TransferScheduler.start(this); if (tab == 1) updateQueue();
        } catch (Exception e) { error(e); }
    }
    private void pause() {
        if (controlsBusy()) return;
        try { store.pauseAll(); sendBroadcast(new Intent(this, TransferScheduler.PauseReceiver.class)); updateQueue(); }
        catch (Exception e) { error(e); }
    }
    private boolean controlsBusy() { return removingJobs || pausingJob || TransferScheduler.busy(); }
    private void pause(String id) {
        if (controlsBusy()) return;
        pausingJob = true; if (tab == 1) updateQueue(); if (detailsDialog != null) updateDetails(); Context app = getApplicationContext();
        new Thread(() -> {
            Exception failure = null;
            try { if (TransferScheduler.pause(app, id)) TransferScheduler.start(app); } catch (Exception e) { failure = e; android.util.Log.e("TVMON", "개별 일시정지 실패", e); }
            Exception result = failure;
            handler.post(() -> { pausingJob = false; if (destroyed) return; if (tab == 1) updateQueue(); if (detailsDialog != null) updateDetails(); if (result != null) error(result); });
        }, "tvmon-pause-one").start();
    }
    private void confirmRemoval(String title, String message, String positive, Runnable action) {
        AlertDialog confirm = new AlertDialog.Builder(this).setTitle(title).setMessage(message).setNegativeButton("취소", null).setPositiveButton(positive, (d, w) -> action.run()).create();
        // Apply the background before the first layout so its padding cannot recenter a visible dialog.
        confirm.getWindow().setBackgroundDrawable(background(Color.WHITE, 20));
        confirm.show();
        confirm.getButton(AlertDialog.BUTTON_POSITIVE).setTextColor(DANGER);
        confirm.getButton(AlertDialog.BUTTON_NEGATIVE).setTextColor(BLUE);
    }
    private void confirmRemoveJobs(List<DownloadStore.Job> selected, boolean all) {
        if (selected.isEmpty() || controlsBusy()) return;
        boolean running = selected.stream().anyMatch(job -> DownloadStore.RUNNING.equals(job.state));
        String title = all ? "대기열을 전체 삭제할까요?" : running ? "다운로드를 중단하고 목록에서 제거할까요?" : "목록에서 삭제할까요?";
        String message = (all ? "대기열 " + selected.size() + "건을 제거합니다.\n" : selected.get(0).title + "\n") + (running ? "진행 중인 다운로드를 중단합니다.\n" : "") + "작업 기록과 미완료 조각을 삭제합니다.\n완료한 영상 파일과 최근 받은 작품은 보관됩니다.";
        List<String> ids = new ArrayList<>(); for (DownloadStore.Job job : selected) ids.add(job.id);
        confirmRemoval(title, message, all ? "전체 삭제" : running ? "중단하고 제거" : "목록에서 삭제", () -> removeJobs(ids));
    }
    private void removeJobs(List<String> ids) {
        if (controlsBusy()) return;
        removingJobs = true; if (detailsDialog != null && ids.contains(detailsId)) detailsDialog.dismiss(); if (tab == 1) updateQueue();
        Context app = getApplicationContext();
        new Thread(() -> {
            Exception failure = null;
            try {
                if (TransferScheduler.remove(app, ids)) try { TransferScheduler.start(app); } catch (Exception e) { throw new java.io.IOException("목록은 제거했지만 남은 다운로드를 시작하지 못했습니다. 대기열에서 다시 시작해 주세요.", e); }
            } catch (Exception e) { failure = e; android.util.Log.e("TVMON", "목록 제거 실패", e); }
            Exception result = failure;
            handler.post(() -> {
                removingJobs = false;
                if (destroyed) return;
                if (tab == 1) updateQueue();
                if (result != null) error(result); else Toast.makeText(this, "목록에서 제거했습니다. 완료한 영상은 보관됩니다.", Toast.LENGTH_SHORT).show();
            });
        }, "tvmon-remove").start();
    }

    private void showSettings() {
        LinearLayout layout = page("설정", "영상 사이트의 현재 접속 주소를 관리하세요.");
        ScrollView scroll = new ScrollView(this); LinearLayout form = column();
        while (layout.getChildCount() > 0) { View child = layout.getChildAt(0); layout.removeViewAt(0); form.addView(child); }
        scroll.addView(form); layout.addView(scroll, new LinearLayout.LayoutParams(-1, 0, 1));
        LinearLayout addresses = column(); addresses.setPadding(dp(16), dp(16), dp(16), dp(16)); addresses.setBackground(background(Color.WHITE, CARD_RADIUS)); form.addView(addresses);
        TextView label = text("접속 도메인", 16, INK, true); addresses.addView(label);
        TextView help = text("한 줄에 하나씩 입력하세요. 위쪽 주소부터 연결합니다.", 14, MUTED, false); help.setPadding(0, dp(8), 0, dp(12)); addresses.addView(help);
        EditText domains = input("https://tvmon1.com"); domainInput = domains; domains.setId(View.generateViewId()); label.setLabelFor(domains.getId()); domains.setSingleLine(false); domains.setMinLines(3); domains.setGravity(Gravity.TOP); domains.setInputType(android.text.InputType.TYPE_CLASS_TEXT | android.text.InputType.TYPE_TEXT_FLAG_MULTI_LINE | android.text.InputType.TYPE_TEXT_VARIATION_URI); domains.setText(draftDomains != null ? draftDomains : settings().getString("domains", SiteClient.DEFAULT_DOMAINS)); addresses.addView(domains, new LinearLayout.LayoutParams(-1, -2));
        TextView guideLabel = text("안내 페이지 주소", 16, INK, true); guideLabel.setPadding(0, dp(24), 0, dp(8)); addresses.addView(guideLabel);
        EditText guide = input(SiteClient.GUIDE); guideInput = guide; guide.setId(View.generateViewId()); guideLabel.setLabelFor(guide.getId()); guide.setSingleLine(true); guide.setInputType(android.text.InputType.TYPE_CLASS_TEXT | android.text.InputType.TYPE_TEXT_VARIATION_URI); guide.setText(draftGuide != null ? draftGuide : settings().getString("guide", SiteClient.GUIDE)); addresses.addView(guide, new LinearLayout.LayoutParams(-1, -2));
        TextView guideHelp = text("현재 주소 항목과 바로가기 링크가 있는 HTTPS 안내 페이지입니다. 수정한 주소는 아래 저장 버튼을 눌러 적용하세요.", 14, MUTED, false); guideHelp.setPadding(0, dp(8), 0, 0); addresses.addView(guideHelp);
        Button save = button("주소 설정 저장", true); LinearLayout.LayoutParams margin = new LinearLayout.LayoutParams(-1, -2); margin.topMargin = dp(16); addresses.addView(save, margin);
        TextView savedFeedback = text("", 14, SUCCESS, false); savedFeedback.setVisibility(View.GONE); savedFeedback.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE); addresses.addView(savedFeedback);
        save.setOnClickListener(v -> {
            savedFeedback.setText(""); savedFeedback.setVisibility(View.GONE);
            String guideValue;
            try { guideValue = SiteClient.guideUrl(guide.getText().toString()); } catch (Exception e) { guide.setError(DownloadEngine.message(e)); guide.requestFocus(); return; }
            try {
                List<String> values = SiteClient.domains(domains.getText().toString()); String value = TextUtils.join("\n", values);
                if (DownloadEngine.running() && !values.equals(SiteClient.domains(settings().getString("domains", SiteClient.DEFAULT_DOMAINS)))) throw new Exception("다운로드를 일시정지한 뒤 도메인을 변경해 주세요.");
                if (!settings().edit().putString("domains", value).putString("guide", guideValue).commit()) throw new Exception("설정을 저장하지 못했습니다.");
                draftDomains = value; draftGuide = guideValue; domains.setText(value); guide.setText(guideValue); savedFeedback.setPadding(0, dp(12), 0, 0); savedFeedback.setVisibility(View.VISIBLE); savedFeedback.setText("주소 설정을 저장했습니다."); Toast.makeText(this, "주소 설정을 저장했습니다.", Toast.LENGTH_SHORT).show(); checkSiteConnection(null, null);
            } catch (Exception e) { domains.setError(DownloadEngine.message(e)); domains.requestFocus(); }
        });
        LinearLayout checks = column(); checks.setPadding(dp(16), dp(16), dp(16), dp(16)); checks.setBackground(background(Color.WHITE, CARD_RADIUS)); LinearLayout.LayoutParams checkSection = new LinearLayout.LayoutParams(-1, -2); checkSection.topMargin = dp(16); form.addView(checks, checkSection);
        TextView checkLabel = text("연결 확인", 16, INK, true); checkLabel.setPadding(0, 0, 0, dp(12)); checks.addView(checkLabel);
        Button discover = button("안내 페이지에서 현재 주소 확인", false); checks.addView(discover, new LinearLayout.LayoutParams(-1, -2));
        TextView feedback = text("", 14, MUTED, false); feedback.setPadding(0, dp(8), 0, 0); checks.addView(feedback);
        feedback.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE);
        discover.setOnClickListener(v -> {
            String guideValue;
            try { guideValue = SiteClient.guideUrl(guide.getText().toString()); } catch (Exception e) { guide.setError(DownloadEngine.message(e)); guide.requestFocus(); return; }
            loading = false;
            discover.setEnabled(false); feedback.setText("안내 페이지의 연결 주소를 확인하고 있습니다…"); SiteClient.Stop stop = beginRequest();
            network.execute(() -> {
                try { String domain = client().currentDomain(guideValue, stop); handler.post(() -> { if (current(stop)) { discover.setEnabled(true); domains.setText(domain + "\nhttps://tvmon1.com"); feedback.setText("안내 주소 " + domain + "의 연결을 확인했습니다. ‘주소 설정 저장’을 눌러 적용하세요."); } }); }
                catch (Exception e) { handler.post(() -> { if (current(stop)) { discover.setEnabled(true); feedback.setText(DownloadEngine.message(e)); } }); }
            });
        });
        Button check = button("사이트 접속 확인", false); connectionButton = check; LinearLayout.LayoutParams checkParams = new LinearLayout.LayoutParams(-1, -2); checkParams.topMargin = dp(16); checks.addView(check, checkParams);
        connectionFeedback = text("저장된 접속 도메인으로 홈페이지 연결을 확인합니다.", 14, MUTED, false); connectionFeedback.setPadding(0, dp(8), 0, 0); checks.addView(connectionFeedback); check.setOnClickListener(v -> checkSiteConnection(connectionFeedback, check));
        connectionFeedback.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE);
        TextView info = text("저장 위치\n다운로드/TVMON\n\nAndroid 10 이상 · TVMON " + BuildConfig.VERSION_NAME + "\n영상 검색 · HTTP 다운로드 · 이어받기", 14, MUTED, false); info.setPadding(0, dp(28), 0, dp(20)); form.addView(info);
    }

    private void rememberSettingsDraft() {
        if (tab == 2 && domainInput != null && guideInput != null) { draftDomains = domainInput.getText().toString(); draftGuide = guideInput.getText().toString(); }
    }

    private void markConnected(String domain) {
        if (domain.isEmpty()) return;
        connectionRevision++;
        lastConnectionCheck = android.os.SystemClock.elapsedRealtime();
        siteStatus.setText("연결됨 · " + Uri.parse(domain).getHost()); siteStatus.setTextColor(0xff236646); siteStatus.setBackground(background(0xffe7f4ed, 10));
        siteStatus.setContentDescription("사이트 연결됨, 실제 접속 주소 " + domain + ", 누르면 설정으로 이동"); siteStatus.setTooltipText("접속 확인: " + domain);
    }
    private void markConnectionFailure(Exception error) {
        connectionRevision++;
        lastConnectionCheck = android.os.SystemClock.elapsedRealtime();
        siteStatus.setText("사이트 연결 실패"); siteStatus.setTextColor(0xff9a3c20); siteStatus.setBackground(background(0xfffff1ec, 10)); siteStatus.setTooltipText(DownloadEngine.message(error)); siteStatus.setContentDescription("사이트 연결 실패, 누르면 설정에서 확인");
    }
    private void checkSiteConnection(TextView feedback, Button trigger) {
        TextView target = feedback != null ? feedback : tab == 2 ? connectionFeedback : null;
        Button action = trigger != null ? trigger : tab == 2 ? connectionButton : null;
        connectionStop.cancel(); SiteClient.Stop stop = new SiteClient.Stop(); connectionStop = stop;
        int revision = ++connectionRevision;
        lastConnectionCheck = android.os.SystemClock.elapsedRealtime();
        siteStatus.setText("사이트 확인 중…"); siteStatus.setTextColor(BLUE); siteStatus.setBackground(background(0xffe5edfa, 10));
        siteStatus.setContentDescription("사이트 확인 중, 누르면 설정으로 이동");
        if (action != null) action.setEnabled(false); if (target != null) { target.setTextColor(MUTED); target.setText("저장된 사이트의 홈페이지를 확인하고 있습니다…"); }
        network.execute(() -> {
            try {
                String domain = client().checkConnection(stop);
                handler.post(() -> { if (!destroyed && connectionStop == stop && !stop.stopped()) {
                    if (action != null) action.setEnabled(true);
                    if (connectionRevision == revision) {
                        markConnected(domain);
                        if (target != null) { target.setTextColor(0xff236646); target.setText("연결됨 · " + domain + "\n홈페이지 확인 " + new java.text.SimpleDateFormat("HH:mm:ss", Locale.KOREA).format(new java.util.Date())); }
                    }
                } });
            } catch (Exception e) { handler.post(() -> { if (!destroyed && connectionStop == stop && !stop.stopped()) {
                if (action != null) action.setEnabled(true);
                if (connectionRevision == revision) { markConnectionFailure(e); if (target != null) { target.setTextColor(0xff9a3c20); target.setText("연결 실패\n" + DownloadEngine.message(e)); } }
            } }); }
        });
    }

    private void back() {
        if (tab != 0) { showTab(0); return; }
        if (!contentUrl.isEmpty()) { beginRequest(); loading = false; contentUrl = ""; episodes.clear(); selected.clear(); content.removeAllViews(); showSearch(); }
        else finish();
    }
    // Android 13+ are handled by the native OnBackInvokedDispatcher registered above.
    @android.annotation.SuppressLint("GestureBackNavigation")
    @Override public void onBackPressed() { if (Build.VERSION.SDK_INT < 33) back(); else super.onBackPressed(); }
    @Override protected void onSaveInstanceState(Bundle state) {
        rememberSettingsDraft();
        super.onSaveInstanceState(state); state.putInt("tab", tab); state.putString("term", searchInput != null && contentUrl.isEmpty() ? searchInput.getText().toString() : term);
        state.putString("content", contentUrl); state.putString("title", contentTitle); state.putStringArrayList("selected", new ArrayList<>(selected));
        state.putString("thumbnail", contentThumbnail);
        state.putBoolean("recent", showingRecent);
        state.putString("draftDomains", draftDomains); state.putString("draftGuide", draftGuide);
        if (detailsId != null) state.putString("details", detailsId);
    }
    @Override protected void onStart() { super.onStart(); handler.removeCallbacks(refreshDetails); if (detailsDialog != null && detailsDialog.isShowing()) handler.post(refreshDetails); }
    @Override protected void onResume() { super.onResume(); if (siteStatus != null && android.os.SystemClock.elapsedRealtime() - lastConnectionCheck > 60000) checkSiteConnection(null, null); }
    @Override protected void onStop() { handler.removeCallbacks(refreshDetails); super.onStop(); }
    @Override protected void onDestroy() { destroyed = true; if (detailsDialog != null) detailsDialog.dismiss(); if (libraryMenuDialog != null) libraryMenuDialog.dismiss(); request.cancel(); connectionStop.cancel(); recentThumbnailStop.cancel(); network.shutdownNow(); handler.removeCallbacksAndMessages(null); unregisterReceiver(receiver); super.onDestroy(); }
    private void error(Exception error) { if (!destroyed) new AlertDialog.Builder(this).setTitle("확인이 필요합니다").setMessage(DownloadEngine.message(error)).setPositiveButton("확인", null).show(); }
    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }
    private LinearLayout column() { LinearLayout value = new LinearLayout(this); value.setOrientation(LinearLayout.VERTICAL); return value; }
    private LinearLayout row() { LinearLayout value = new LinearLayout(this); value.setOrientation(LinearLayout.HORIZONTAL); return value; }
    private LinearLayout.LayoutParams weighted() { LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(0, -2, 1); params.setMargins(dp(SPACE), 0, dp(SPACE), 0); return params; }
    private TextView text(String value, int size, int color, boolean bold) { TextView view = new TextView(this); view.setText(value); view.setTextSize(size); view.setTextColor(color); view.setFontFeatureSettings("kern"); if (bold) view.setTypeface(Typeface.DEFAULT, Typeface.BOLD); return view; }
    private GradientDrawable background(int color, int radius) { GradientDrawable value = new GradientDrawable(); value.setColor(color); value.setCornerRadius(dp(radius)); return value; }
    private EditText input(String hint) { EditText view = new EditText(this); view.setHint(hint); view.setTextSize(16); view.setTextColor(INK); view.setHintTextColor(MUTED); view.setMinHeight(dp(52)); view.setMinimumHeight(dp(52)); view.setPadding(dp(12), dp(12), dp(12), dp(12)); GradientDrawable border = background(Color.WHITE, CARD_RADIUS); border.setStroke(dp(1), OUTLINE); GradientDrawable focus = background(Color.WHITE, CARD_RADIUS); focus.setStroke(dp(2), BLUE); StateListDrawable states = new StateListDrawable(); states.addState(new int[]{android.R.attr.state_focused}, focus); states.addState(new int[]{}, border); view.setBackground(states); return view; }
    private android.graphics.drawable.Drawable selectableBackground(int color, int stroke) { GradientDrawable fill = background(color, CARD_RADIUS); if (stroke != 0) fill.setStroke(dp(1), stroke); return new android.graphics.drawable.RippleDrawable(android.content.res.ColorStateList.valueOf(0x18245eb8), fill, null); }
    private Button button(String label, boolean primary) {
        Button button = new Button(this); button.setText(label); button.setAllCaps(false); button.setTextSize(14); button.setTypeface(Typeface.DEFAULT, Typeface.BOLD); button.setMinHeight(dp(TOUCH)); button.setMinimumHeight(dp(TOUCH)); button.setMinWidth(dp(TOUCH)); button.setMinimumWidth(dp(TOUCH)); button.setPadding(dp(12), dp(8), dp(12), dp(8));
        android.content.res.ColorStateList colors = new android.content.res.ColorStateList(new int[][]{new int[]{-android.R.attr.state_enabled}, new int[]{}}, new int[]{0xff6c7a8c, primary ? Color.WHITE : BLUE}); button.setTextColor(colors);
        StateListDrawable states = new StateListDrawable(); states.addState(new int[]{-android.R.attr.state_enabled}, background(DISABLED, CARD_RADIUS)); states.addState(new int[]{}, selectableBackground(primary ? BLUE : SOFT_BLUE, 0));
        button.setBackground(states); button.setStateListAnimator(null); button.setElevation(0); return button;
    }
    private ListView list(LinearLayout layout) {
        ListView view = new ListView(this); view.setDivider(new android.graphics.drawable.ColorDrawable(BACKGROUND)); view.setDividerHeight(dp(8)); view.setClipToPadding(false); view.setPadding(0, 0, 0, dp(8));
        LinearLayout header = column();
        while (layout.getChildCount() > 0) { View child = layout.getChildAt(0); layout.removeViewAt(0); header.addView(child); }
        if (header.getChildCount() > 0) view.addHeaderView(header, null, false); layout.addView(view, new LinearLayout.LayoutParams(-1, 0, 1)); return view;
    }
    private static String bytes(long value) { return value >= 1024L * 1024 * 1024 ? String.format(Locale.KOREA, "%.1f GB", value / (1024.0 * 1024 * 1024)) : String.format(Locale.KOREA, "%.1f MB", value / (1024.0 * 1024)); }
}
