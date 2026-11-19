package lk.gov.aiplatform.monitor

import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.BatteryManager
import android.os.Build
import androidx.work.Worker
import androidx.work.WorkerParameters
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.UUID
import java.util.concurrent.TimeUnit

/** Network-only recovery heartbeat. Never captures or uploads screen contents. */
class RecoveryWorker(appContext: Context, params: WorkerParameters) : Worker(appContext, params) {
    override fun doWork(): Result {
        val prefs = applicationContext.getSharedPreferences("monitor", Context.MODE_PRIVATE)
        val baseUrl = prefs.getString("service_url", null)?.trim()?.trimEnd('/') ?: return Result.success()
        val apiKey = prefs.getString("api_key", null)?.trim() ?: return Result.success()
        if (!baseUrl.startsWith("https://") || apiKey.length < 32) return Result.success()

        val deviceId = prefs.getString("device_id", null) ?: UUID.randomUUID().toString().also {
            prefs.edit().putString("device_id", it).apply()
        }
        val batteryIntent = applicationContext.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        val level = batteryIntent?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1
        val scale = batteryIntent?.getIntExtra(BatteryManager.EXTRA_SCALE, 100) ?: 100
        val battery = if (level >= 0 && scale > 0) level * 100 / scale else -1
        val display = applicationContext.resources.displayMetrics
        val payload = JSONObject()
            .put("device_id", deviceId)
            .put("device_name", Build.DEVICE)
            .put("manufacturer", Build.MANUFACTURER)
            .put("model", Build.MODEL)
            .put("android_version", Build.VERSION.RELEASE)
            .put("battery_percent", if (battery >= 0) battery else JSONObject.NULL)
            .put("network_type", "Connected")
            .put("screen_resolution", "${display.widthPixels}x${display.heightPixels}")

        val client = OkHttpClient.Builder()
            .connectTimeout(20, TimeUnit.SECONDS)
            .readTimeout(20, TimeUnit.SECONDS)
            .callTimeout(30, TimeUnit.SECONDS)
            .build()
        return try {
            val request = Request.Builder()
                .url("$baseUrl/v1/heartbeat")
                .header("X-Monitor-Key", apiKey)
                .post(payload.toString().toRequestBody("application/json".toMediaType()))
                .build()
            client.newCall(request).execute().use { response ->
                prefs.edit().putString(
                    "status",
                    if (response.isSuccessful) "Connected - background heartbeat"
                    else "Background heartbeat HTTP ${response.code}"
                ).apply()
                if (response.isSuccessful || response.code in 400..499) Result.success() else Result.retry()
            }
        } catch (e: Exception) {
            prefs.edit().putString("status", "Waiting for network/server: ${e.javaClass.simpleName}").apply()
            Result.retry()
        } finally {
            client.dispatcher.executorService.shutdown()
            client.connectionPool.evictAll()
        }
    }
}
