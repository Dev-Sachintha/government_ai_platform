
package lk.gov.aiplatform.monitor

import android.app.Activity
import android.content.Intent
import android.media.projection.MediaProjectionManager
import android.os.Bundle
import android.widget.*
import android.view.ViewGroup

class MainActivity : Activity() {

    private val requestCapture = 901

    private lateinit var url: EditText
    private lateinit var key: EditText

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
