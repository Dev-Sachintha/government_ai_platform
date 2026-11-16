package lk.gov.aiplatform.monitor

import android.app.*
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.Bitmap
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.*
import android.provider.Settings
import android.util.DisplayMetrics
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.util.UUID
import java.util.concurrent.TimeUnit

class MonitorService : Service() {
    private val handler = Handler(Looper.getMainLooper())
    private val client = OkHttpClient.Builder().callTimeout(12, TimeUnit.SECONDS).build()
    private var projection: MediaProjection? = null
    private var virtualDisplay: VirtualDisplay? = null
    private var reader: ImageReader? = null
    private var baseUrl = ""
    private var apiKey = ""
    private val deviceId by lazy { getSharedPreferences("monitor", MODE_PRIVATE).getString("device_id", null) ?: UUID.randomUUID().toString().also { getSharedPreferences("monitor", MODE_PRIVATE).edit().putString("device_id", it).apply() } }
    private val capture = object : Runnable { override fun run() { captureAndUpload(); handler.postDelayed(this, 1500) } }
    override fun onBind(intent: Intent?) = null
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        baseUrl = intent?.getStringExtra("baseUrl") ?: return START_NOT_STICKY
        apiKey = intent.getStringExtra("apiKey") ?: return START_NOT_STICKY
        val resultCode = intent.getIntExtra("resultCode", Activity.RESULT_CANCELED)
        val data = if (Build.VERSION.SDK_INT >= 33) intent.getParcelableExtra("data", Intent::class.java) else @Suppress("DEPRECATION") intent.getParcelableExtra("data")
        if (resultCode != Activity.RESULT_OK || data == null) { stopSelf(); return START_NOT_STICKY }
        createNotification()
        startProjection(resultCode, data)
        handler.post(capture)
        return START_NOT_STICKY
    }
    private fun createNotification() {
        val channel = "phone_monitor"
        if (Build.VERSION.SDK_INT >= 26) { val nm = getSystemService(NotificationManager::class.java); nm.createNotificationChannel(NotificationChannel(channel, "Screen sharing", NotificationManager.IMPORTANCE_LOW)) }
        val stopIntent = PendingIntent.getService(this, 1, Intent(this, MonitorService::class.java).setAction("STOP"), PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val notification = Notification.Builder(this, channel).setContentTitle("Screen sharing is active").setContentText("Your phone screen is being shared with the Government AI Platform").setSmallIcon(android.R.drawable.ic_menu_view).addAction(Notification.Action.Builder(null, "Stop sharing", stopIntent).build()).build()
        if (Build.VERSION.SDK_INT >= 29) startForeground(7, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION) else startForeground(7, notification)
    }
    private fun startProjection(code: Int, data: Intent) {
        val manager = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        projection = manager.getMediaProjection(code, data)
        val metrics = DisplayMetrics(); @Suppress("DEPRECATION") val display = (getSystemService(WINDOW_SERVICE) as WindowManager).defaultDisplay; @Suppress("DEPRECATION") display.getRealMetrics(metrics)
        val width = metrics.widthPixels.coerceAtMost(1280); val height = (metrics.heightPixels * (width.toFloat() / metrics.widthPixels)).toInt().coerceAtLeast(1)
        reader = ImageReader.newInstance(width, height, android.graphics.PixelFormat.RGBA_8888, 2)
        virtualDisplay = projection?.createVirtualDisplay("GovAIMonitor", width, height, metrics.densityDpi, DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR, reader?.surface, null, handler)
        projection?.registerCallback(object : MediaProjection.Callback() { override fun onStop() { stopSelf() } }, handler)
    }
    private fun captureAndUpload() {
        try {
            val image = reader?.acquireLatestImage() ?: return
            image.use {
                val plane = it.planes[0]; val buffer = plane.buffer; val pixelStride = plane.pixelStride; val rowStride = plane.rowStride
                val rowPadding = rowStride - pixelStride * it.width
                val full = Bitmap.createBitmap(it.width + rowPadding / pixelStride, it.height, Bitmap.Config.ARGB_8888)
                full.copyPixelsFromBuffer(buffer)
                val cropped = Bitmap.createBitmap(full, 0, 0, it.width, it.height); full.recycle()
                val out = ByteArrayOutputStream(); cropped.compress(Bitmap.CompressFormat.JPEG, 55, out); cropped.recycle()
                uploadFrame(out.toByteArray())
            }
        } catch (_: Exception) { }
        sendHeartbeat()
    }
    private fun sendHeartbeat() {
        try {
            val battery = registerReceiver(null, android.content.IntentFilter(Intent.ACTION_BATTERY_CHANGED))
            val level = battery?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1
            val scale = battery?.getIntExtra(BatteryManager.EXTRA_SCALE, 100) ?: 100
            val pct = if (level >= 0 && scale > 0) (level * 100 / scale) else -1
            val dm = resources.displayMetrics
            val obj = JSONObject().put("device_id", deviceId).put("device_name", android.os.Build.DEVICE).put("manufacturer", Build.MANUFACTURER).put("model", Build.MODEL).put("android_version", Build.VERSION.RELEASE).put("battery_percent", if (pct >= 0) pct else JSONObject.NULL).put("network_type", "Android network").put("screen_resolution", "${dm.widthPixels}x${dm.heightPixels}")
            val req = Request.Builder().url("$baseUrl/v1/heartbeat").header("X-Monitor-Key", apiKey).post(obj.toString().toRequestBody("application/json".toMediaType())).build()
            client.newCall(req).execute().close()
        } catch (_: Exception) { }
    }
    private fun uploadFrame(bytes: ByteArray) {
        val body = MultipartBody.Builder().setType(MultipartBody.FORM).addFormDataPart("device_id", deviceId).addFormDataPart("frame", "screen.jpg", bytes.toRequestBody("image/jpeg".toMediaType())).build()
        val req = Request.Builder().url("$baseUrl/v1/frame").header("X-Monitor-Key", apiKey).post(body).build()
        try { client.newCall(req).execute().close() } catch (_: Exception) { }
    }
    override fun onDestroy() { handler.removeCallbacks(capture); virtualDisplay?.release(); reader?.close(); projection?.stop(); stopForeground(STOP_FOREGROUND_REMOVE); super.onDestroy() }
}
