package com.rickyid666.bililivelike;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import org.json.JSONObject;

/**
 * 主界面: 本地 HTML (响应式界面) + JS 桥接调用原生点赞引擎。
 * 登录走应用内 WebView, 登录成功后自动抓取 Cookie, 不需要扫码/粘贴。
 * 任务运行期间启动前台服务, 保证息屏/切后台也能继续点赞。
 */
public class MainActivity extends Activity {

    private static final int REQ_LOGIN = 1001;
    private static final int REQ_NOTI = 1002;

    private WebView web;
    private LikeEngine engine;
    private long lastNotiUpdate = 0;
    private volatile boolean serviceOn = false;

    @SuppressLint({"SetJavaScriptEnabled", "AddJavascriptInterface"})
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        CookieManager.getInstance().setAcceptCookie(true);

        engine = new LikeEngine(this);
        engine.setListener(line -> {
            runOnUiThread(() -> evalLog(line));
            maybeUpdateNotification();
        });

        web = new WebView(this);
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, true);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(true);
        s.setDatabaseEnabled(true);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);
        web.setWebChromeClient(new WebChromeClient());
        web.setWebViewClient(new WebViewClient());
        web.setBackgroundColor(0xFFEEF0F3);
        web.addJavascriptInterface(new Bridge(), "Android");

        setContentView(web);
        web.loadUrl("file:///android_asset/index.html");

        ensureNotificationPermission();
        new Thread(this::pushAccount).start();
    }

    /** Android 13+ 需要通知权限才能看到前台服务的常驻通知 */
    private void ensureNotificationPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
                && checkSelfPermission("android.permission.POST_NOTIFICATIONS")
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"}, REQ_NOTI);
        }
    }

    private void startForegroundServiceForTask(String text) {
        if (!serviceOn) {
            serviceOn = true;
            LikeService.start(this, text);
        }
    }

    private void stopForegroundService() {
        if (serviceOn) {
            serviceOn = false;
            LikeService.stop(this);
        }
    }

    /** 通知文案刷新做节流, 避免刷得太频繁 */
    private void maybeUpdateNotification() {
        long now = System.currentTimeMillis();
        if (now - lastNotiUpdate < 3000) {
            return;
        }
        lastNotiUpdate = now;
        if (engine.running()) {
            LikeService.update("已点赞 " + engine.status().optInt("likes") + " 次");
        }
    }

    private void pushAccount() {
        boolean ok = engine.checkLogin();
        String js = "window.__onAccount(" + JSONObject.quote(
                "{\"logged_in\":" + ok + ",\"uname\":" + JSONObject.quote(engine.uname()) + "}")
                + ");";
        runOnUiThread(() -> eval(js));
    }

    private void eval(String js) {
        if (web != null) {
            web.evaluateJavascript(js, null);
        }
    }

    private void evalLog(String line) {
        eval("window.__log(" + JSONObject.quote(line) + ");");
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == REQ_LOGIN && resultCode == RESULT_OK) {
            String ck = LikeEngine.collectFromWebView();
            if (ck != null && !ck.isEmpty()) {
                engine.saveCookie(ck);
            }
            new Thread(() -> {
                engine.checkLogin();
                engine.log("[login] 登录成功 · " + engine.uname());
                pushAccount();
            }).start();
        }
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) {
            web.goBack();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (!engine.running()) {
            stopForegroundService();
        }
    }

    @Override
    protected void onDestroy() {
        engine.stop();
        stopForegroundService();
        if (web != null) {
            web.destroy();
            web = null;
        }
        super.onDestroy();
    }

    /** 暴露给网页的桥接: JS 侧统一通过 call(action, jsonPayload) 调用 */
    private class Bridge {

        @JavascriptInterface
        public String call(String action, String payload) {
            JSONObject in;
            try {
                in = new JSONObject(payload == null || payload.isEmpty() ? "{}" : payload);
            } catch (Exception e) {
                in = new JSONObject();
            }
            JSONObject out = new JSONObject();
            try {
                switch (action == null ? "" : action) {
                    case "status":
                        out = engine.status();
                        // 任务自然结束(达标/登录失效)时收掉前台服务
                        if (!engine.running() && serviceOn) {
                            runOnUiThread(this::stopForegroundService);
                        }
                        break;

                    case "logs": {
                        int since = in.optInt("since", 0);
                        out.put("ok", true);
                        out.put("lines", engine.logsSince(since));
                        out.put("next", engine.logCount());
                        break;
                    }

                    case "login":
                        runOnUiThread(() -> startActivityForResult(
                                new Intent(MainActivity.this, LoginActivity.class), REQ_LOGIN));
                        out.put("ok", true);
                        break;

                    case "cookie": {
                        String raw = in.optString("cookie", "");
                        if (raw.trim().isEmpty()) {
                            out.put("ok", false);
                            out.put("error", "Cookie 内容为空");
                            break;
                        }
                        engine.saveCookie(raw);
                        boolean ok = engine.checkLogin();
                        out.put("ok", ok);
                        out.put("uname", engine.uname());
                        if (ok) {
                            engine.log("[cookie] 登录成功 · " + engine.uname());
                        } else {
                            out.put("error", "Cookie 无效或已过期");
                        }
                        break;
                    }

                    case "logout":
                        engine.clearCookie();
                        out.put("ok", true);
                        break;

                    case "room":
                        out = engine.resolveRoom(in.optString("q", ""));
                        break;

                    case "start":
                        out = engine.start(in.optString("room", ""), in);
                        if (out.optBoolean("ok")) {
                            final String t = "已启动 · 房间 " + out.optString("room");
                            runOnUiThread(() -> startForegroundServiceForTask(t));
                        }
                        break;

                    case "stop":
                        engine.stop();
                        runOnUiThread(this::stopForegroundService);
                        out.put("ok", true);
                        break;

                    case "exit":
                        runOnUiThread(MainActivity.this::finish);
                        out.put("ok", true);
                        break;

                    default:
                        out.put("ok", false);
                        out.put("error", "未知操作: " + action);
                }
            } catch (Exception e) {
                try {
                    out = new JSONObject();
                    out.put("ok", false);
                    out.put("error", String.valueOf(e.getMessage()));
                } catch (Exception ignored) {
                }
            }
            return out.toString();
        }
    }
}
