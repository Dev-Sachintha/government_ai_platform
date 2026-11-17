
package lk.gov.aiplatform.monitor

import android.Manifest
import android.app.Activity
import android.content.pm.PackageManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import java.util.UUID
import android.content.Intent
import android.media.projection.MediaProjectionManager
import android.os.Bundle
import android.widget.*
import android.view.ViewGroup

class MainActivity : Activity() {

    private val requestCapture = 901

    private lateinit var url: EditText
    private lateinit var key: EditText
    private lateinit var status: TextView

    private val ui = Handler(Looper.getMainLooper())
    private val refreshStatus = object : Runnable {
        override fun run() {
            val saved = getSharedPreferences("monitor", MODE_PRIVATE)
                .getString("status", "Not sharing") ?: "Not sharing"
            status.text = "Status: $saved"
            ui.postDelayed(this, 2000)
        }
    }

    override fun onResume() {
        super.onResume()
        ui.post(refreshStatus)
    }

    override fun onPause() {
        ui.removeCallbacks(refreshStatus)
        super.onPause()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val prefs = getSharedPreferences("monitor", MODE_PRIVATE)

        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(32, 40, 32, 24)
        }

        layout.addView(TextView(this).apply {
            text = """
                Government AI Platform
                Mobile Phone Monitor

                Enter your monitoring service URL and pairing key.

                Screen sharing starts only after you approve Android's permission.
            """.trimIndent()
            textSize = 18f
        })

        url = EditText(this).apply {
            hint = "https://your-monitor-service.onrender.com"
            setSingleLine(true)
            setText(prefs.getString("service_url", ""))
        }

        layout.addView(url)

        key = EditText(this).apply {
            hint = "Pairing key"
            setSingleLine(true)
            setText(prefs.getString("api_key", ""))
        }

        layout.addView(key)

        val startButton = Button(this).apply {
            text = "Start Screen Sharing"
        }

        layout.addView(startButton)

        val stopButton = Button(this).apply {
            text = "Stop Screen Sharing"
        }

        layout.addView(stopButton)

        val testButton = Button(this).apply {
            text = "Test connection"
        }

        layout.addView(testButton)

        status = TextView(this).apply {
            text = "Status: Not sharing"
            textSize = 16f
            setPadding(0, 16, 0, 16)
        }

        layout.addView(status)

        layout.addView(TextView(this).apply {
            text = """
                Privacy information:

                • Screen sharing requires your permission.
                • Device information is sent to the monitoring server.
                • Android displays a screen-sharing notification.
                • You can stop sharing at any time.
                • No microphone or camera recording is performed.
            """.trimIndent()
            textSize = 14f
        })

        setContentView(
            ScrollView(this).apply {
                addView(layout)
            }
        )

        // Android 13+: the notification that tells the user "screen sharing is active" needs this permission.
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 902)
        }

        testButton.setOnClickListener {
            val base = url.text.toString().trim().trimEnd('/')
            val secret = key.text.toString().trim()

            if (!base.startsWith("https://") || secret.length < 32) {
                Toast.makeText(this, "Enter a valid HTTPS URL and pairing key", Toast.LENGTH_LONG).show()
                return@setOnClickListener
            }

            status.text = "Status: testing the connection... (a sleeping server can take a minute)"
            ui.removeCallbacks(refreshStatus)

            val deviceId = prefs.getString("device_id", null)
                ?: UUID.randomUUID().toString().also { prefs.edit().putString("device_id", it).apply() }

            Thread {
                val result = ConnectionTester.run(base, secret, deviceId)
                runOnUiThread {
                    status.text = "Status: $result"
                    // keep this result visible for a while before the live status takes over again
                    ui.postDelayed(refreshStatus, 15000)
                }
            }.start()
        }

        startButton.setOnClickListener {

            val base = url.text.toString()
                .trim()
                .trimEnd('/')

            val secret = key.text.toString().trim()

            if (!base.startsWith("https://") || secret.length < 32) {
                Toast.makeText(
                    this,
                    "Enter a valid HTTPS URL and pairing key",
                    Toast.LENGTH_LONG
                ).show()

                return@setOnClickListener
            }

            prefs.edit()
                .putString("service_url", base)
                .putString("api_key", secret)
                .apply()

            val manager = getSystemService(
                MEDIA_PROJECTION_SERVICE
            ) as MediaProjectionManager

            startActivityForResult(
                manager.createScreenCaptureIntent(),
                requestCapture
            )
        }

        stopButton.setOnClickListener {
            stopService(
                Intent(this, MonitorService::class.java)
            )

            Toast.makeText(
                this,
                "Screen sharing stopped",
                Toast.LENGTH_SHORT
            ).show()
        }
    }

    @Deprecated("Android activity result compatibility")
    override fun onActivityResult(
        requestCode: Int,
        resultCode: Int,
        data: Intent?
    ) {
        super.onActivityResult(requestCode, resultCode, data)

        if (requestCode == requestCapture) {

            if (resultCode == RESULT_OK && data != null) {

                val service = Intent(
                    this,
                    MonitorService::class.java
                ).apply {

                    putExtra("resultCode", resultCode)
                    putExtra("data", data)

                    putExtra(
                        "baseUrl",
                        url.text.toString().trim().trimEnd('/')
                    )

                    putExtra(
                        "apiKey",
                        key.text.toString().trim()
                    )
                }

                try {
                    startForegroundService(service)

                    Toast.makeText(
                        this,
                        "Screen sharing started",
                        Toast.LENGTH_SHORT
                    ).show()

                } catch (e: Exception) {
                    Toast.makeText(
                        this,
                        "Unable to start monitoring: ${e.message}",
                        Toast.LENGTH_LONG
                    ).show()
                }

            } else {

                Toast.makeText(
                    this,
                    "Screen capture permission was not granted",
                    Toast.LENGTH_LONG
                ).show()
            }
        }
    }
}
