package com.rickyid666.bililivelike;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Intent;
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
 */
public class MainActivity extends Activity {

    private static final int REQ_LOGIN = 1001;

    private WebView web;
    private LikeEngine engine;

    @SuppressLint({"SetJavaScriptEnabled", "AddJavascriptInterface"})
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        CookieManager.getInstance().setAcceptCookie(true);

        engine = new LikeEngine(this);
        engine.setListener(line -> runOnUiThread(() -> evalLog(line)));

        web = new WebView(this);
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, true);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(true);
        s.setDatabaseEnabled(true);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);
        // 让本地页面里的按钮点击有即时反馈
        web.setWebChromeClient(new WebChromeClient());
        web.setWebViewClient(new WebViewClient());
        web.setBackgroundColor(0xFFEEF0F3);
        web.addJavascriptInterface(new Bridge(), "Android");

        setContentView(web);
        web.loadUrl("file:///android_asset/index.html");

        // 启动后异步校验一次登录态, 结果推给页面
        new Thread(this::pushAccount).start();
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
                        break;

                    case "stop":
                        engine.stop();
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
