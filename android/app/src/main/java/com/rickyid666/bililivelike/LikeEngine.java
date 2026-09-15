package com.rickyid666.bililivelike;

import android.content.Context;
import android.content.SharedPreferences;
import android.webkit.CookieManager;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Random;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 点赞引擎: 直接调 B 站官方接口 (与手动双击点赞同一接口)。
 * 全部网络请求在调用线程内同步执行, 由 JS 桥接的后台线程负责调用, 不阻塞界面。
 */
public class LikeEngine {

    public interface Listener {
        void onLog(String line);
    }

    private static final String UA =
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            + "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36";

    private static final String NAV = "https://api.bilibili.com/x/web-interface/nav";
    private static final String ROOM_INIT =
            "https://api.live.bilibili.com/room/v1/Room/room_init?id=";
    private static final String LIKE =
            "https://api.live.bilibili.com/xlive/app-ucenter/v1/like_info_v3/like/likeReportV3";

    private static final Pattern P_DIGIT = Pattern.compile("^\\d+$");
    private static final Pattern P_URL = Pattern.compile("live\\.bilibili\\.com/(?:h5/)?(\\d+)");

    private static final int MAX_LOGS = 600;

    private final SharedPreferences prefs;
    private final Random rnd = new Random();
    private final List<String> logs = new ArrayList<>();
    private Listener listener;

    private Thread worker;
    private volatile boolean running = false;
    private volatile boolean stopFlag = false;
    private volatile int likes = 0;
    private volatile int clicks = 0;
    private volatile long startTs = 0;
    private volatile String room = "";
    private volatile String uname = "";
    private volatile boolean loggedIn = false;

    // 运行参数 (可从界面调整)
    public volatile double ivMin = 5.0;
    public volatile double ivMax = 8.0;
    public volatile int ckMin = 10;
    public volatile int ckMax = 20;
    public volatile int maxLikes = 1000;
    public volatile boolean waitLive = true;   // 未开播时轮询等待
    private static final int WAIT_INTERVAL_MS = 60 * 1000;

    private String cookie = "";
    private String csrf = "";
    private String uid = "";

    public LikeEngine(Context ctx) {
        prefs = ctx.getSharedPreferences("bll", Context.MODE_PRIVATE);
        cookie = prefs.getString("cookie", "");
        uname = prefs.getString("uname", "");
        room = prefs.getString("room", "");
        ivMin = prefs.getFloat("ivMin", 5f);
        ivMax = prefs.getFloat("ivMax", 8f);
        ckMin = prefs.getInt("ckMin", 10);
        ckMax = prefs.getInt("ckMax", 20);
        maxLikes = prefs.getInt("maxLikes", 1000);
        waitLive = prefs.getBoolean("waitLive", true);
    }

    public void setListener(Listener l) {
        this.listener = l;
    }

    public void log(String line) {
        synchronized (logs) {
            logs.add(line);
            while (logs.size() > MAX_LOGS) {
                logs.remove(0);
            }
        }
        Listener l = listener;
        if (l != null) {
            l.onLog(line);
        }
    }

    /** 增量日志: 返回 [index, text] 列表 */
    public JSONArray logsSince(int since) {
        JSONArray arr = new JSONArray();
        synchronized (logs) {
            for (int i = Math.max(0, since); i < logs.size(); i++) {
                JSONObject o = new JSONObject();
                try {
                    o.put("i", i);
                    o.put("text", logs.get(i));
                } catch (Exception ignored) {
                }
                arr.put(o);
            }
        }
        return arr;
    }

    public int logCount() {
        synchronized (logs) {
            return logs.size();
        }
    }

    // ================= Cookie =================
    public String cookie() {
        if (cookie == null || cookie.isEmpty()) {
            cookie = prefs.getString("cookie", "");
        }
        return cookie == null ? "" : cookie;
    }

    /** 从 WebView 的 cookie 罐收集 (登录页登录成功后使用) */
    public static String collectFromWebView() {
        CookieManager cm = CookieManager.getInstance();
        StringBuilder sb = new StringBuilder();
        String[] urls = {
                "https://www.bilibili.com",
                "https://api.bilibili.com",
                "https://api.live.bilibili.com",
                "https://live.bilibili.com",
        };
        List<String> seen = new ArrayList<>();
        for (String u : urls) {
            String c = cm.getCookie(u);
            if (c == null || c.isEmpty()) {
                continue;
            }
            for (String part : c.split(";")) {
                String p = part.trim();
                if (p.isEmpty()) {
                    continue;
                }
                String name = p.contains("=") ? p.substring(0, p.indexOf('=')) : p;
                if (!seen.contains(name)) {
                    seen.add(name);
                    if (sb.length() > 0) {
                        sb.append("; ");
                    }
                    sb.append(p);
                }
            }
        }
        return sb.toString();
    }

    public void saveCookie(String raw) {
        String c = raw == null ? "" : raw.trim();
        if (c.startsWith("Cookie:")) {
            c = c.substring(7).trim();
        }
        cookie = c;
        prefs.edit().putString("cookie", c).apply();
        // 同步写进 WebView 的 cookie 罐, 保证两边一致
        CookieManager cm = CookieManager.getInstance();
        cm.setAcceptCookie(true);
        for (String part : c.split(";")) {
            String p = part.trim();
            if (!p.isEmpty() && p.contains("=")) {
                cm.setCookie("https://www.bilibili.com", p);
                cm.setCookie("https://api.bilibili.com", p);
                cm.setCookie("https://api.live.bilibili.com", p);
            }
        }
        cm.flush();
    }

    public void clearCookie() {
        cookie = "";
        uname = "";
        loggedIn = false;
        prefs.edit().remove("cookie").remove("uname").apply();
        CookieManager cm = CookieManager.getInstance();
        cm.removeAllCookies(null);
        cm.flush();
    }

    private String cookieValue(String name) {
        for (String part : cookie().split(";")) {
            String p = part.trim();
            int eq = p.indexOf('=');
            if (eq > 0 && p.substring(0, eq).trim().equals(name)) {
                return p.substring(eq + 1).trim();
            }
        }
        return "";
    }

    // ================= HTTP =================
    private JSONObject request(String method, String url, String body, String referer)
            throws Exception {
        HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
        conn.setRequestMethod(method);
        conn.setConnectTimeout(12000);
        conn.setReadTimeout(20000);
        conn.setRequestProperty("User-Agent", UA);
        conn.setRequestProperty("Accept", "application/json, text/plain, */*");
        conn.setRequestProperty("Accept-Language", "zh-CN,zh;q=0.9");
        conn.setRequestProperty("Cookie", cookie());
        if (referer != null) {
            conn.setRequestProperty("Referer", referer);
            conn.setRequestProperty("Origin", "https://live.bilibili.com");
        }
        conn.setDoInput(true);
        if (body != null) {
            conn.setDoOutput(true);
            conn.setRequestProperty("Content-Type",
                    "application/x-www-form-urlencoded; charset=UTF-8");
        }
        conn.connect();
        if (body != null) {
            try (OutputStream os = conn.getOutputStream()) {
                os.write(body.getBytes(StandardCharsets.UTF_8));
            }
        }
        int code = conn.getResponseCode();
        InputStream is = code >= 400 ? conn.getErrorStream() : conn.getInputStream();
        String text = readAll(is);
        conn.disconnect();
        if (text == null || text.trim().isEmpty()) {
            JSONObject o = new JSONObject();
            o.put("code", code);
            o.put("message", "empty body (HTTP " + code + ")");
            return o;
        }
        return new JSONObject(text);
    }

    private static String readAll(InputStream is) {
        if (is == null) {
            return "";
        }
        try (ByteArrayOutputStream bos = new ByteArrayOutputStream()) {
            byte[] buf = new byte[8192];
            int n;
            while ((n = is.read(buf)) > 0) {
                bos.write(buf, 0, n);
            }
            return bos.toString("UTF-8");
        } catch (Exception e) {
            return "";
        }
    }

    private static String form(java.util.Map<String, String> kv) {
        StringBuilder sb = new StringBuilder();
        for (java.util.Map.Entry<String, String> e : kv.entrySet()) {
            if (sb.length() > 0) {
                sb.append('&');
            }
            sb.append(URLEncoder.encode(e.getKey(), StandardCharsets.UTF_8));
            sb.append('=');
            sb.append(URLEncoder.encode(e.getValue(), StandardCharsets.UTF_8));
        }
        return sb.toString();
    }

    public static String normalizeRoom(String raw) {
        if (raw == null) {
            return "";
        }
        String s = raw.trim();
        if (s.isEmpty()) {
            return "";
        }
        if (P_DIGIT.matcher(s).matches()) {
            return s;
        }
        Matcher m = P_URL.matcher(s);
        return m.find() ? m.group(1) : "";
    }

    // ================= 账号 =================
    /** 校验登录态, 更新缓存 (会发起网络请求) */
    public boolean checkLogin() {
        try {
            JSONObject r = request("GET", NAV, null, null);
            JSONObject d = r.optJSONObject("data");
            boolean ok = d != null && d.optBoolean("isLogin", false);
            loggedIn = ok;
            if (ok) {
                uname = d.optString("uname", "");
                prefs.edit().putString("uname", uname).apply();
            }
            return ok;
        } catch (Exception e) {
            loggedIn = false;
            return false;
        }
    }

    public String uname() {
        return uname;
    }

    public boolean loggedIn() {
        return loggedIn;
    }

    // ================= 房间 =================
    public JSONObject resolveRoom(String raw) {
        String r = normalizeRoom(raw == null || raw.isEmpty() ? room : raw);
        JSONObject out = new JSONObject();
        try {
            if (r.isEmpty()) {
                out.put("ok", false);
                out.put("error", "房间号格式不对");
                return out;
            }
            JSONObject res = request("GET", ROOM_INIT + r, null, null);
            if (res.optInt("code", -1) != 0) {
                out.put("ok", false);
                out.put("error", "房间解析失败: " + res.optString("message"));
                return out;
            }
            JSONObject d = res.getJSONObject("data");
            out.put("ok", true);
            out.put("room_id", d.optLong("room_id"));
            out.put("anchor_id", d.optLong("uid"));
            out.put("live_status", d.optInt("live_status", 0));
            out.put("short", r);
            return out;
        } catch (Exception e) {
            try {
                out.put("ok", false);
                out.put("error", "网络异常: " + e.getMessage());
            } catch (Exception ignored) {
            }
            return out;
        }
    }

    // ================= 点赞 =================
    private JSONObject likeOnce(long roomId, long anchorId, String shortRoom) throws Exception {
        int clickTime = ckMin + rnd.nextInt(Math.max(1, ckMax - ckMin + 1));
        java.util.Map<String, String> kv = new java.util.LinkedHashMap<>();
        kv.put("click_time", String.valueOf(clickTime));
        kv.put("room_id", String.valueOf(roomId));
        kv.put("anchor_id", String.valueOf(anchorId));
        kv.put("uid", uid);
        kv.put("csrf_token", csrf);
        kv.put("csrf", csrf);
        kv.put("visit_id", "");
        JSONObject r = request("POST", LIKE, form(kv),
                "https://live.bilibili.com/" + shortRoom);
        r.put("_click", clickTime);
        return r;
    }

    public JSONObject start(String rawRoom, JSONObject cfg) {
        JSONObject out = new JSONObject();
        try {
            if (running) {
                out.put("ok", false);
                out.put("error", "已在运行中");
                return out;
            }
            if (cfg != null) {
                ivMin = Math.max(1.0, cfg.optDouble("interval_min", ivMin));
                ivMax = Math.max(ivMin, cfg.optDouble("interval_max", ivMax));
                ckMin = Math.max(1, cfg.optInt("click_min", ckMin));
                ckMax = Math.max(ckMin, cfg.optInt("click_max", ckMax));
                maxLikes = Math.max(0, cfg.optInt("max_likes", maxLikes));
                waitLive = cfg.optBoolean("wait_live", waitLive);
                prefs.edit()
                        .putFloat("ivMin", (float) ivMin)
                        .putFloat("ivMax", (float) ivMax)
                        .putInt("ckMin", ckMin)
                        .putInt("ckMax", ckMax)
                        .putInt("maxLikes", maxLikes)
                        .putBoolean("waitLive", waitLive)
                        .apply();
            }
            if (!checkLogin()) {
                out.put("ok", false);
                out.put("error", "未登录，请先在上方登录哔哩哔哩");
                return out;
            }
            csrf = cookieValue("bili_jct");
            uid = cookieValue("DedeUserID");
            if (csrf.isEmpty()) {
                out.put("ok", false);
                out.put("error", "Cookie 缺少 bili_jct，请重新登录");
                return out;
            }
            String r = normalizeRoom(rawRoom == null ? room : rawRoom);
            if (r.isEmpty()) {
                out.put("ok", false);
                out.put("error", "房间号格式不对");
                return out;
            }
            room = r;
            prefs.edit().putString("room", r).apply();
            stopFlag = false;
            running = true;
            final String fRoom = r;
            worker = new Thread(() -> taskLoop(fRoom), "like-worker");
            worker.start();
            out.put("ok", true);
            out.put("room", r);
            out.put("uname", uname);
            return out;
        } catch (Exception e) {
            try {
                out.put("ok", false);
                out.put("error", "启动失败: " + e.getMessage());
            } catch (Exception ignored) {
            }
            return out;
        }
    }

    private void taskLoop(String shortRoom) {
        try {
            JSONObject info = resolveRoom(shortRoom);
            if (!info.optBoolean("ok")) {
                log("[task] " + info.optString("error"));
                running = false;
                return;
            }
            long roomId = info.optLong("room_id");
            long anchorId = info.optLong("anchor_id");
            if (info.optInt("live_status", 0) == 0) {
                if (!waitLive) {
                    log("[task] 该直播间当前未开播，点赞无效，任务停止");
                    running = false;
                    return;
                }
                // 等开播: 每分钟查一次房间状态
                log("[task] 该直播间当前未开播，每分钟自动检查一次…（可在参数里关闭「自动等开播」）");
                boolean liveNow = false;
                int waited = 0;
                while (!stopFlag) {
                    for (int i = 0; i < WAIT_INTERVAL_MS / 1000 && !stopFlag; i++) {
                        Thread.sleep(1000);
                    }
                    if (stopFlag) {
                        break;
                    }
                    waited += WAIT_INTERVAL_MS / 60000;
                    JSONObject again = resolveRoom(shortRoom);
                    if (again.optBoolean("ok") && again.optInt("live_status", 0) == 1) {
                        roomId = again.optLong("room_id");
                        anchorId = again.optLong("anchor_id");
                        liveNow = true;
                        break;
                    }
                    log("[task] 还没开播，已等待 " + waited + " 分钟…");
                }
                if (stopFlag) {
                    running = false;
                    return;
                }
                if (liveNow) {
                    log("[task] 检测到已开播，开始点赞");
                }
            }
            likes = 0;
            clicks = 0;
            startTs = System.currentTimeMillis();
            log(String.format(Locale.US, "[task] 目标房间 %d（开播中），开始点赞", roomId));
            log(String.format(Locale.US,
                    "[task] 间隔 %.1f~%.1fs，每次连击 %d~%d 赞，上限 %d",
                    ivMin, ivMax, ckMin, ckMax, maxLikes));
            while (!stopFlag) {
                double span = Math.max(0.1, ivMax - ivMin);
                long wait = (long) ((ivMin + rnd.nextDouble() * span) * 1000);
                long end = System.currentTimeMillis() + wait;
                while (System.currentTimeMillis() < end && !stopFlag) {
                    Thread.sleep(100);
                }
                if (stopFlag) {
                    break;
                }
                try {
                    JSONObject r = likeOnce(roomId, anchorId, shortRoom);
                    int code = r.optInt("code");
                    clicks++;
                    if (code == 0) {
                        int c = r.optInt("_click", 0);
                        likes += c;
                        log(String.format(Locale.US, "[task] 第%d次请求 OK (+%d 赞，累计 %d)",
                                clicks, c, likes));
                    } else if (code == -101) {
                        log("[task] 登录已失效，请重新登录，任务停止");
                        break;
                    } else if (code == -352) {
                        log("[task] 触发风控(-352)，自动放慢速度…");
                        ivMin = Math.min(ivMin + 3, 30);
                        ivMax = Math.min(ivMax + 5, 60);
                        prefs.edit().putFloat("ivMin", (float) ivMin)
                                .putFloat("ivMax", (float) ivMax).apply();
                        Thread.sleep(10000);
                    } else {
                        log("[task] 返回异常 code=" + code + " " + r.optString("message"));
                        Thread.sleep(3000);
                    }
                    if (maxLikes > 0 && likes >= maxLikes) {
                        log("[task] 已达单场点赞上限(" + maxLikes + ")，任务完成，自动停止");
                        break;
                    }
                } catch (Exception e) {
                    log("[task] 网络异常: " + e.getMessage() + "，重试中");
                    Thread.sleep(3000);
                }
            }
        } catch (InterruptedException ignored) {
        } catch (Exception e) {
            log("[task] 任务异常: " + e.getMessage());
        } finally {
            running = false;
            log("[task] 点赞已停止");
        }
    }

    public void stop() {
        if (running) {
            stopFlag = true;
            log("[ui] 正在停止…");
        }
    }

    public boolean running() {
        return running;
    }

    public JSONObject status() {
        JSONObject o = new JSONObject();
        try {
            o.put("ok", true);
            o.put("logged_in", loggedIn || !cookie().isEmpty());
            o.put("uname", uname);
            o.put("running", running);
            o.put("likes", likes);
            o.put("clicks", clicks);
            o.put("room", room);
            o.put("elapsed", running && startTs > 0
                    ? (System.currentTimeMillis() - startTs) / 1000 : 0);
            o.put("log_count", logCount());
            o.put("iv_min", ivMin);
            o.put("iv_max", ivMax);
            o.put("ck_min", ckMin);
            o.put("ck_max", ckMax);
            o.put("max_likes", maxLikes);
            o.put("wait_live", waitLive);
        } catch (Exception ignored) {
        }
        return o;
    }
}
