package com.rickyid666.bililivelike;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.os.Bundle;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.LinearLayout;
import android.widget.TextView;

/**
 * 应用内登录页: 直接加载 B 站网页登录页, 用自己的账号/短信登录。
 * 登录成功(出现 SESSDATA)后自动抓 Cookie 并返回主界面, 全程不需要扫码或复制。
 */
public class LoginActivity extends Activity {

    private static final String MOBILE_UA =
            "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
            + "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36";

    private WebView web;
    private boolean done = false;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        CookieManager.getInstance().setAcceptCookie(true);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);

        TextView tip = new TextView(this);
        tip.setText("在下面完成哔哩哔哩登录（账号密码 / 短信验证码），登录成功后会自动返回");
        tip.setTextSize(12f);
        tip.setPadding(28, 22, 28, 22);
        tip.setBackgroundColor(0xFFFFF0F4);
        tip.setTextColor(0xFFC7305A);
        root.addView(tip, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        web = new WebView(this);
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, true);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setUserAgentString(MOBILE_UA);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView view, String url) {
                maybeFinish();
            }
        });
        root.addView(web, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));

        setContentView(root);
        web.loadUrl("https://passport.bilibili.com/login");
    }

    /** 页面每次加载完成后检查登录态, 拿到 SESSDATA 就算成功 */
    private void maybeFinish() {
        if (done) {
            return;
        }
        String ck = LikeEngine.collectFromWebView();
        if (ck != null && ck.contains("SESSDATA=")) {
            done = true;
            setResult(RESULT_OK);
            finish();
        }
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack() && !done) {
            web.goBack();
        } else {
            setResult(RESULT_CANCELED);
            super.onBackPressed();
        }
    }

    @Override
    protected void onDestroy() {
        if (web != null) {
            web.destroy();
            web = null;
        }
        super.onDestroy();
    }
}
