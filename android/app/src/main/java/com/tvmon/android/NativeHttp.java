package com.tvmon.android;

import android.util.Base64;
import java.io.IOException;
import java.security.KeyStore;
import java.security.cert.X509Certificate;
import javax.net.ssl.TrustManager;
import javax.net.ssl.TrustManagerFactory;
import javax.net.ssl.X509TrustManager;

/** The desktop ECH/DoH transport, called only after a site's normal connection fails. */
final class NativeHttp {
    static { System.loadLibrary("tvmon_http"); }
    private static String authorities;

    static SiteClient.Response read(String url, String referer, int limit, SiteClient.Stop stop) throws IOException {
        stop.check();
        java.net.URI target = SiteClient.checked(url), ref = SiteClient.checked(referer);
        boolean localTest = BuildConfig.DEBUG && "http".equals(target.getScheme()) && "127.0.0.1".equals(target.getHost());
        if (limit < 0) throw new IOException("서버 응답의 허용 크기가 올바르지 않습니다.");
        SiteClient.Response response = request(url, referer, ref.getScheme() + "://" + ref.getRawAuthority(),
                SiteClient.AGENT, localTest ? "" : authorities(), limit, stop, localTest);
        stop.check(); SiteClient.checked(response.url()); return response;
    }

    private static synchronized String authorities() throws IOException {
        if (authorities != null) return authorities;
        try {
            TrustManagerFactory factory = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());
            factory.init((KeyStore) null);
            StringBuilder pem = new StringBuilder();
            for (TrustManager manager : factory.getTrustManagers()) if (manager instanceof X509TrustManager trusted) {
                for (X509Certificate certificate : trusted.getAcceptedIssuers()) pem.append("-----BEGIN CERTIFICATE-----\n")
                        .append(Base64.encodeToString(certificate.getEncoded(), Base64.DEFAULT)).append("-----END CERTIFICATE-----\n");
            }
            if (pem.length() == 0) throw new IOException("Android의 신뢰할 수 있는 인증서를 읽지 못했습니다.");
            authorities = pem.toString(); return authorities;
        } catch (java.security.GeneralSecurityException error) { throw new IOException("Android 인증서를 준비하지 못했습니다.", error); }
    }

    static native String features();
    private static native SiteClient.Response request(String url, String referer, String origin, String agent,
                                                      String authorities, int limit, SiteClient.Stop stop, boolean localTest) throws IOException;
}
