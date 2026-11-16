
package lk.gov.aiplatform.monitor

import android.app.*
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.ServiceInfo
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.*
import android.util.DisplayMetrics
import android.view.WindowManager

import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody

import org.json.JSONObject

import java.io.ByteArrayOutputStream
import java.util.UUID
import java.util.concurrent.TimeUnit

class MonitorService : Service() {

    private val handler = Handler(Looper.getMainLooper())

    private val client = OkHttpClient.Builder()
        .callTimeout(12, TimeUnit.SECONDS)
        .build()

    private var projection: MediaProjection? = null
    private var virtualDisplay: VirtualDisplay? = null
    private var reader: ImageReader? = null

    private var baseUrl = ""
    private var apiKey = ""

    private val deviceId: String by lazy {

        val prefs = getSharedPreferences("monitor", MODE_PRIVATE)

        prefs.getString("device_id", null)
            ?: UUID.randomUUID().toString().also {
                prefs.edit()
                    .putString("device_id", it)
                    .apply()
            }
    }

    private var isCapturing = false

    private val captureTask = object : Runnable {

        override fun run() {

            if (!isCapturing) return

            captureAndUpload()

            handler.postDelayed(this, 1500)
        }
    }

    override fun onBind(intent: Intent?) = null

    override fun onStartCommand(
        intent: Intent?,
        flags: Int,
        startId: Int
    ): Int {

        if (intent?.action == "STOP") {
            stopSharing()
            return START_NOT_STICKY
        }

        if (isCapturing) {
            return START_NOT_STICKY
        }

        baseUrl = intent?.getStringExtra("baseUrl")
            ?: run {
                stopSelf()
                return START_NOT_STICKY
            }

        apiKey = intent.getStringExtra("apiKey")
            ?: run {
                stopSelf()
                return START_NOT_STICKY
            }

        val resultCode = intent.getIntExtra(
            "resultCode",
            Activity.RESULT_CANCELED
        )

        val data = if (Build.VERSION.SDK_INT >= 33) {
            intent.getParcelableExtra("data", Intent::class.java)
        } else {
            @Suppress("DEPRECATION")
            intent.getParcelableExtra("data")
        }

        if (resultCode != Activity.RESULT_OK || data == null) {
            stopSelf()
            return START_NOT_STICKY
        }

        createNotification()

        startProjection(resultCode, data)

        if (projection == null || reader == null) {
            stopSelf()
            return START_NOT_STICKY
        }

        isCapturing = true

        handler.post(captureTask)

        return START_NOT_STICKY
    }

    private fun createNotification() {

        val channelId = "phone_monitor"

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {

            val manager =
                getSystemService(NotificationManager::class.java)

            val channel = NotificationChannel(
                channelId,
                "Mobile Screen Sharing",
                NotificationManager.IMPORTANCE_LOW
            )

            manager.createNotificationChannel(channel)
        }

        val stopIntent = PendingIntent.getService(
            this,
            1,
            Intent(this, MonitorService::class.java).apply {
                action = "STOP"
            },
            PendingIntent.FLAG_UPDATE_CURRENT or
                PendingIntent.FLAG_IMMUTABLE
        )

        val notification = Notification.Builder(this, channelId)
            .setContentTitle("Screen sharing is active")
            .setContentText(
                "Your phone screen is being shared with the Government AI Platform"
            )
            .setSmallIcon(android.R.drawable.ic_menu_view)
            .addAction(
                Notification.Action.Builder(
                    null,
                    "Stop sharing",
                    stopIntent
                ).build()
            )
            .setOngoing(true)
            .build()

        if (Build.VERSION.SDK_INT >= 29) {

            startForeground(
                7,
                notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION
            )

        } else {

            startForeground(7, notification)
        }
    }

    private fun startProjection(
        resultCode: Int,
        data: Intent
    ) {

        val manager = getSystemService(
            Context.MEDIA_PROJECTION_SERVICE
        ) as MediaProjectionManager

        projection = manager.getMediaProjection(
            resultCode,
            data
        )

        val metrics = getScreenMetrics()

        val originalWidth = metrics.widthPixels.coerceAtLeast(1)
        val originalHeight = metrics.heightPixels.coerceAtLeast(1)

        val width = originalWidth.coerceAtMost(1280)

        val height = (
            originalHeight.toFloat() *
                width.toFloat() /
                originalWidth.toFloat()
            ).toInt().coerceAtLeast(1)

        reader = ImageReader.newInstance(
            width,
            height,
            PixelFormat.RGBA_8888,
            3
        )

        projection?.registerCallback(
            object : MediaProjection.Callback() {

                override fun onStop() {
                    isCapturing = false
                    stopSelf()
                }
            },
            handler
        )

        virtualDisplay = projection?.createVirtualDisplay(
            "GovernmentAIPhoneMonitor",
            width,
            height,
            metrics.densityDpi,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
            reader?.surface,
            null,
            handler
        )
    }

    private fun getScreenMetrics(): DisplayMetrics {

        val metrics = DisplayMetrics()

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {

            val windowManager =
                getSystemService(WindowManager::class.java)

            val bounds = windowManager.currentWindowMetrics.bounds

            metrics.widthPixels = bounds.width()
            metrics.heightPixels = bounds.height()
            metrics.densityDpi = resources.displayMetrics.densityDpi

        } else {

            @Suppress("DEPRECATION")
            val windowManager =
                getSystemService(Context.WINDOW_SERVICE) as WindowManager

            @Suppress("DEPRECATION")
            windowManager.defaultDisplay.getRealMetrics(metrics)
        }

        return metrics
    }

    private fun captureAndUpload() {

        try {

            val image = reader?.acquireLatestImage() ?: run {
                sendHeartbeat()
                return
            }

            image.use {

                val plane = it.planes[0]

                val buffer = plane.buffer
                val pixelStride = plane.pixelStride
                val rowStride = plane.rowStride

                val rowPadding =
                    rowStride - pixelStride * it.width

                val fullBitmap = Bitmap.createBitmap(
                    it.width + rowPadding / pixelStride,
                    it.height,
                    Bitmap.Config.ARGB_8888
                )

                fullBitmap.copyPixelsFromBuffer(buffer)

                val croppedBitmap = Bitmap.createBitmap(
                    fullBitmap,
                    0,
                    0,
                    it.width,
                    it.height
                )

                fullBitmap.recycle()

                val output = ByteArrayOutputStream()

                croppedBitmap.compress(
                    Bitmap.CompressFormat.JPEG,
                    55,
                    output
                )

                croppedBitmap.recycle()

                uploadFrame(output.toByteArray())
            }

        } catch (_: Exception) {
            // A temporary frame failure should not crash the service.
        }

        sendHeartbeat()
    }

    private fun sendHeartbeat() {

        try {

            val battery = registerReceiver(
                null,
                IntentFilter(Intent.ACTION_BATTERY_CHANGED)
            )

            val level = battery?.getIntExtra(
                BatteryManager.EXTRA_LEVEL,
                -1
            ) ?: -1

            val scale = battery?.getIntExtra(
                BatteryManager.EXTRA_SCALE,
                100
            ) ?: 100

            val percentage = if (level >= 0 && scale > 0) {
                level * 100 / scale
            } else {
                -1
            }

            val metrics = resources.displayMetrics

            val deviceInfo = JSONObject()
                .put("device_id", deviceId)
                .put("device_name", Build.DEVICE)
                .put("manufacturer", Build.MANUFACTURER)
                .put("model", Build.MODEL)
                .put("android_version", Build.VERSION.RELEASE)
                .put(
                    "battery_percent",
                    if (percentage >= 0) percentage else JSONObject.NULL
                )
                .put("network_type", "Android network")
                .put(
                    "screen_resolution",
                    "${metrics.widthPixels}x${metrics.heightPixels}"
                )

            val request = Request.Builder()
                .url("$baseUrl/v1/heartbeat")
                .header("X-Monitor-Key", apiKey)
                .post(
                    deviceInfo.toString().toRequestBody(
                        "application/json".toMediaType()
                    )
                )
                .build()

            client.newCall(request).execute().close()

        } catch (_: Exception) {
            // Network errors are retried on the next heartbeat.
        }
    }

    private fun uploadFrame(bytes: ByteArray) {

        val body = MultipartBody.Builder()
            .setType(MultipartBody.FORM)
            .addFormDataPart("device_id", deviceId)
            .addFormDataPart(
                "frame",
                "screen.jpg",
                bytes.toRequestBody("image/jpeg".toMediaType())
            )
            .build()

        val request = Request.Builder()
            .url("$baseUrl/v1/frame")
            .header("X-Monitor-Key", apiKey)
            .post(body)
            .build()

        try {
            client.newCall(request).execute().close()
        } catch (_: Exception) {
            // Upload will be attempted again during the next capture.
        }
    }

    private fun stopSharing() {

        isCapturing = false

        handler.removeCallbacks(captureTask)

        try {
            virtualDisplay?.release()
            virtualDisplay = null

            reader?.close()
            reader = null

            projection?.stop()
            projection = null

        } catch (_: Exception) {
        }

        stopForeground(STOP_FOREGROUND_REMOVE)

        stopSelf()
    }

    override fun onDestroy() {

        isCapturing = false

        handler.removeCallbacks(captureTask)

        try {
            virtualDisplay?.release()
            reader?.close()
            projection?.stop()
        } catch (_: Exception) {
        }

        super.onDestroy()
    }
}
