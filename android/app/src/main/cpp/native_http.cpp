#include <jni.h>
#include <curl/curl.h>
#include <algorithm>
#include <cctype>
#include <cstdlib>
#include <memory>
#include <string>
#include <vector>

namespace {
struct Request {
    JNIEnv* env;
    jobject stop;
    jmethodID check;
    size_t limit;
    std::vector<unsigned char> body;
    long status = 0;
    bool challenge = false, tooLarge = false;
    bool cancelled() {
        if (env->ExceptionCheck()) return true;
        env->CallVoidMethod(stop, check);
        return env->ExceptionCheck();
    }
};

std::string string(JNIEnv* env, jstring value) {
    const char* text = env->GetStringUTFChars(value, nullptr);
    if (!text) return {};
    std::string copy(text); env->ReleaseStringUTFChars(value, text); return copy;
}
void fail(JNIEnv* env, const std::string& message) {
    if (!env->ExceptionCheck()) env->ThrowNew(env->FindClass("java/io/IOException"), message.c_str());
}
size_t header(char* data, size_t size, size_t count, void* opaque) {
    auto& request = *static_cast<Request*>(opaque);
    if (request.cancelled()) return 0;
    size_t length = size * count;
    std::string line(data, length);
    if (line.rfind("HTTP/", 0) == 0) {
        request.body.clear(); request.challenge = false;
        size_t space = line.find(' ');
        request.status = space == std::string::npos ? 0 : std::strtol(line.c_str() + space + 1, nullptr, 10);
    } else {
        std::transform(line.begin(), line.end(), line.begin(), [](unsigned char ch) { return static_cast<char>(std::tolower(ch)); });
        if (line.rfind("cf-mitigated:", 0) == 0 && line.find("challenge") != std::string::npos) request.challenge = true;
    }
    return length;
}
size_t receive(char* data, size_t size, size_t count, void* opaque) {
    auto& request = *static_cast<Request*>(opaque);
    if (request.cancelled() || request.challenge) return 0;
    size_t length = size * count;
    if (request.status >= 300 && request.status < 400) return length;
    if (length > request.limit - request.body.size()) { request.tooLarge = true; return 0; }
    try { request.body.insert(request.body.end(), data, data + length); }
    catch (const std::bad_alloc&) { return 0; }
    return length;
}
}

extern "C" JNIEXPORT jint JNICALL JNI_OnLoad(JavaVM*, void*) {
    return curl_global_init(CURL_GLOBAL_DEFAULT) == CURLE_OK ? JNI_VERSION_1_6 : JNI_ERR;
}

extern "C" JNIEXPORT jstring JNICALL Java_com_tvmon_android_NativeHttp_features(JNIEnv* env, jclass) {
    std::string value = curl_version();
    const curl_version_info_data* info = curl_version_info(CURLVERSION_NOW);
    if (info->feature_names) for (const char* const* feature = info->feature_names; *feature; ++feature) value += std::string(" ") + *feature;
    return env->NewStringUTF(value.c_str());
}

extern "C" JNIEXPORT jobject JNICALL Java_com_tvmon_android_NativeHttp_request(
        JNIEnv* env, jclass, jstring target, jstring referer, jstring origin, jstring agent,
        jstring authorities, jint limit, jobject stop, jboolean localTest) {
    std::string url = string(env, target), ref = string(env, referer), from = string(env, origin),
                userAgent = string(env, agent), certificates = string(env, authorities);
    if (env->ExceptionCheck()) return nullptr;
    Request request{env, stop, env->GetMethodID(env->GetObjectClass(stop), "check", "()V"), static_cast<size_t>(limit), {}};
    if (env->ExceptionCheck() || request.cancelled()) return nullptr;
    std::unique_ptr<CURL, decltype(&curl_easy_cleanup)> curl(curl_easy_init(), curl_easy_cleanup);
    std::unique_ptr<CURLM, decltype(&curl_multi_cleanup)> multi(curl_multi_init(), curl_multi_cleanup);
    if (!curl || !multi) { fail(env, "ECH/DoH 통신 엔진을 준비하지 못했습니다."); return nullptr; }
    curl_slist* rawHeaders = nullptr;
    for (const std::string& field : {"Referer: " + ref, "Origin: " + from, std::string("Accept: */*"), std::string("Accept-Encoding: identity")}) {
        curl_slist* next = curl_slist_append(rawHeaders, field.c_str());
        if (!next) { curl_slist_free_all(rawHeaders); fail(env, "요청 헤더를 준비하지 못했습니다."); return nullptr; }
        rawHeaders = next;
    }
    std::unique_ptr<curl_slist, decltype(&curl_slist_free_all)> headers(rawHeaders, curl_slist_free_all);
    curl_blob ca{certificates.data(), certificates.size(), CURL_BLOB_COPY};
    CURLcode setup = CURLE_OK;
#define OPTION(key, value) if (setup == CURLE_OK) setup = curl_easy_setopt(curl.get(), key, value)
    OPTION(CURLOPT_URL, url.c_str());
    OPTION(CURLOPT_HTTPHEADER, headers.get());
    OPTION(CURLOPT_USERAGENT, userAgent.c_str());
    OPTION(CURLOPT_PROXY, "");
    OPTION(CURLOPT_PROTOCOLS_STR, localTest ? "http,https" : "https");
    OPTION(CURLOPT_REDIR_PROTOCOLS_STR, "https");
    OPTION(CURLOPT_DISALLOW_USERNAME_IN_URL, 1L);
    OPTION(CURLOPT_FOLLOWLOCATION, 1L);
    OPTION(CURLOPT_MAXREDIRS, 5L);
    OPTION(CURLOPT_CONNECTTIMEOUT_MS, 12000L);
    OPTION(CURLOPT_TIMEOUT_MS, 20000L);
    OPTION(CURLOPT_NOSIGNAL, 1L);
    OPTION(CURLOPT_HTTP_VERSION, CURL_HTTP_VERSION_2TLS);
    OPTION(CURLOPT_SSL_VERIFYPEER, 1L);
    OPTION(CURLOPT_SSL_VERIFYHOST, 2L);
    OPTION(CURLOPT_DOH_SSL_VERIFYPEER, 1L);
    OPTION(CURLOPT_DOH_SSL_VERIFYHOST, 2L);
    if (!localTest) {
        OPTION(CURLOPT_CAINFO_BLOB, &ca);
        OPTION(CURLOPT_DOH_URL, "https://cloudflare-dns.com/dns-query");
        OPTION(CURLOPT_ECH, "true");
    }
    OPTION(CURLOPT_HEADERFUNCTION, header);
    OPTION(CURLOPT_HEADERDATA, &request);
    OPTION(CURLOPT_WRITEFUNCTION, receive);
    OPTION(CURLOPT_WRITEDATA, &request);
#undef OPTION
    if (setup != CURLE_OK) { fail(env, std::string("ECH/DoH 설정 실패: ") + curl_easy_strerror(setup)); return nullptr; }
    CURLMcode state = curl_multi_add_handle(multi.get(), curl.get());
    CURLcode result = CURLE_FAILED_INIT;
    int active = 1;
    while (state == CURLM_OK && active && !request.cancelled()) {
        state = curl_multi_perform(multi.get(), &active);
        if (state == CURLM_OK && active) state = curl_multi_poll(multi.get(), nullptr, 0, 100, nullptr);
    }
    int remaining;
    while (CURLMsg* message = curl_multi_info_read(multi.get(), &remaining)) if (message->msg == CURLMSG_DONE) result = message->data.result;
    curl_multi_remove_handle(multi.get(), curl.get());
    if (env->ExceptionCheck()) return nullptr;
    if (request.challenge) { fail(env, "사이트에서 브라우저 인증을 요청했습니다. 이 영상 전용 버전은 브라우저 인증을 지원하지 않습니다."); return nullptr; }
    if (request.tooLarge) { fail(env, "서버 응답이 허용 크기를 초과했습니다."); return nullptr; }
    if (state != CURLM_OK) { fail(env, std::string("ECH/DoH 연결 실패: ") + curl_multi_strerror(state)); return nullptr; }
    if (result != CURLE_OK) { fail(env, std::string("ECH/DoH 연결 실패: ") + curl_easy_strerror(result)); return nullptr; }
    if (request.status < 200 || request.status >= 300) { fail(env, std::string("ECH/DoH 서버 응답: HTTP ") + std::to_string(request.status)); return nullptr; }
    char* effective = nullptr;
    curl_easy_getinfo(curl.get(), CURLINFO_EFFECTIVE_URL, &effective);
    jbyteArray body = env->NewByteArray(static_cast<jsize>(request.body.size()));
    if (!body) return nullptr;
    if (!request.body.empty()) env->SetByteArrayRegion(body, 0, static_cast<jsize>(request.body.size()), reinterpret_cast<const jbyte*>(request.body.data()));
    jstring resolved = env->NewStringUTF(effective ? effective : url.c_str());
    jclass response = env->FindClass("com/tvmon/android/SiteClient$Response");
    return env->ExceptionCheck() ? nullptr : env->NewObject(response, env->GetMethodID(response, "<init>", "([BLjava/lang/String;)V"), body, resolved);
}
