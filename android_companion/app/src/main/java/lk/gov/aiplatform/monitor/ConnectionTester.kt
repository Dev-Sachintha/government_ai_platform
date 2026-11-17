package lk.gov.aiplatform.monitor

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/** Checks the service URL and pairing key without starting screen sharing. Call from a background thread. */
object ConnectionTester {

    fun run(baseUrl: String, apiKey: String, deviceId: String): String {
        val client = OkHttpClient.Builder()
            .connectTimeout(60, TimeUnit.SECONDS)
            .readTimeout(60, TimeUnit.SECONDS)
            .callTimeout(90, TimeUnit.SECONDS)
            .build()

        try {
            client.newCall(Request.Builder().url("$baseUrl/health").build()).execute().use { r ->
                if (!r.isSuccessful) {
                    return "The server answered HTTP ${r.code} for /health. Check the service URL."
                }
            }
        } catch (e: Exception) {
            return "Cannot reach the server: ${e.javaClass.simpleName} ${e.message ?: ""}".trim() +
                "\n(A free Render service needs up to a minute to wake up. Try again.)"
        }

        val info = JSONObject()
            .put("device_id", deviceId)
            .put("device_name", android.os.Build.DEVICE)
            .put("manufacturer", android.os.Build.MANUFACTURER)
            .put("model", android.os.Build.MODEL)
            .put("android_version", android.os.Build.VERSION.RELEASE)
            .put("network_type", "Android network")

        return try {
            val request = Request.Builder()
                .url("$baseUrl/v1/heartbeat")
                .header("X-Monitor-Key", apiKey)
                .post(info.toString().toRequestBody("application/json".toMediaType()))
                .build()
            client.newCall(request).execute().use { r ->
                when {
                    r.isSuccessful -> "Connected OK. This phone should now appear in the dashboard."
                    r.code == 401 -> "Server reached, but the pairing key is WRONG. It must equal MOBILE_MONITOR_API_KEY on the server and in the Streamlit secrets."
                    else -> "Server reached, but /v1/heartbeat answered HTTP ${r.code}."
                }
            }
        } catch (e: Exception) {
            "Heartbeat failed: ${e.javaClass.simpleName} ${e.message ?: ""}".trim()
        }
    }
}
