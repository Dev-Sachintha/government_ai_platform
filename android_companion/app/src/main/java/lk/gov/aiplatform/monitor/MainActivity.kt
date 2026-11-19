
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
import android.text.InputType
import android.app.AlertDialog
import android.view.ViewGroup

class MainActivity : Activity() {

    private val requestCapture = 901

    private lateinit var status: TextView
    private lateinit var accountLabel: TextView

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
        applyPlatformLaunch(intent)

        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(32, 40, 32, 24)
        }

        layout.addView(TextView(this).apply {
            text = """
                Government AI Platform
                Mobile Phone Monitor

                Screen monitoring is ready.

                Connection details are stored in app-private storage and hidden from the main screen.
                Screen sharing starts only after you approve Android's permission.
            """.trimIndent()
            textSize = 18f
        })

        // Credentials are intentionally not displayed on the main screen.
        // They remain in app-private SharedPreferences and can be changed in Connection Settings.
        accountLabel = TextView(this).apply {
            textSize = 15f
            setPadding(0, 10, 0, 10)
            text = "Platform account: ${prefs.getString("login_user", "Not linked") ?: "Not linked"}"
        }
        layout.addView(accountLabel)

        val settingsButton = Button(this).apply {
            text = "Connection Settings"
        }
        layout.addView(settingsButton)

        val startButton = Button(this).apply {
            text = "Start Screen Sharing (Manual)"
        }

        layout.addView(startButton)

        val autoButton = Button(this).apply {
            text = "Auto Screen Monitoring"
        }
        layout.addView(autoButton)

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

        settingsButton.setOnClickListener {
            val fields = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                setPadding(48, 12, 48, 0)
            }
            val urlInput = EditText(this).apply {
                hint = "Monitoring service URL (HTTPS)"
                setSingleLine(true)
                inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
                setText(prefs.getString("service_url", ""))
            }
            val keyInput = EditText(this).apply {
                hint = "Pairing key"
                setSingleLine(true)
                inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
                setText(prefs.getString("api_key", ""))
            }
            fields.addView(urlInput)
            fields.addView(keyInput)
            AlertDialog.Builder(this)
                .setTitle("Connection Settings")
                .setMessage("Credentials are hidden on the main screen. Update them here only when required.")
                .setView(fields)
                .setNegativeButton("Cancel", null)
                .setPositiveButton("Save") { _, _ ->
                    val base = urlInput.text.toString().trim().trimEnd('/')
                    val secret = keyInput.text.toString().trim()
                    if (!base.startsWith("https://") || secret.length < 32) {
                        Toast.makeText(this, "Configure a valid HTTPS URL and pairing key in Connection Settings", Toast.LENGTH_LONG).show()
                    } else {
                        prefs.edit().putString("service_url", base).putString("api_key", secret).apply()
                        Toast.makeText(this, "Connection details saved", Toast.LENGTH_SHORT).show()
                    }
                }
                .show()
        }

        testButton.setOnClickListener {
            val base = prefs.getString("service_url", "")?.trim()?.trimEnd('/').orEmpty()
            val secret = prefs.getString("api_key", "")?.trim().orEmpty()

            if (!base.startsWith("https://") || secret.length < 32) {
                Toast.makeText(this, "Configure a valid HTTPS URL and pairing key in Connection Settings", Toast.LENGTH_LONG).show()
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

        val beginScreenSharing = {

            val base = prefs.getString("service_url", "")?.trim()?.trimEnd('/').orEmpty()
            val secret = prefs.getString("api_key", "")?.trim().orEmpty()

            if (!base.startsWith("https://") || secret.length < 32) {
                Toast.makeText(
                    this,
                    "Connection details are not configured. Open Connection Settings first.",
                    Toast.LENGTH_LONG
                ).show()
            } else {
                val manager = getSystemService(
                    MEDIA_PROJECTION_SERVICE
                ) as MediaProjectionManager

                startActivityForResult(
                    manager.createScreenCaptureIntent(),
                    requestCapture
                )
            }
        }

        startButton.setOnClickListener { beginScreenSharing() }
        autoButton.setOnClickListener {
            // Uses the already-saved service URL and pairing key; no re-entry is required.
            val savedBase = prefs.getString("service_url", "")?.trim().orEmpty()
            val savedKey = prefs.getString("api_key", "")?.trim().orEmpty()
            if (savedBase.isBlank() || savedKey.length < 32) {
                Toast.makeText(this, "Connect this phone once using the service URL and pairing key first.", Toast.LENGTH_LONG).show()
                return@setOnClickListener
            }
            beginScreenSharing()
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

    override fun onNewIntent(intent: Intent?) {
        super.onNewIntent(intent)
        setIntent(intent)
        applyPlatformLaunch(intent)
        val user = getSharedPreferences("monitor", MODE_PRIVATE)
            .getString("login_user", "Not linked") ?: "Not linked"
        if (::accountLabel.isInitialized) accountLabel.text = "Platform account: $user"
    }

    private fun applyPlatformLaunch(intent: Intent?) {
        val uri = intent?.data ?: return
        if (uri.scheme != "govmonitor" || uri.host != "monitor") return
        val user = uri.getQueryParameter("login_user")?.trim()?.take(80).orEmpty()
        val serviceUrl = uri.getQueryParameter("service_url")?.trim()?.trimEnd('/').orEmpty()
        val prefs = getSharedPreferences("monitor", MODE_PRIVATE)
        prefs.edit().apply {
            if (user.isNotBlank()) putString("login_user", user)
            if (serviceUrl.startsWith("https://")) putString("service_url", serviceUrl)
        }.apply()
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
                        getSharedPreferences("monitor", MODE_PRIVATE).getString("service_url", "")
                    )

                    putExtra(
                        "apiKey",
                        getSharedPreferences("monitor", MODE_PRIVATE).getString("api_key", "")
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
