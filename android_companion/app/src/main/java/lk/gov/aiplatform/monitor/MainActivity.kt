package lk.gov.aiplatform.monitor

import android.app.Activity
import android.content.Intent
import android.media.projection.MediaProjectionManager
import android.os.Bundle
import android.widget.*
import android.app.Activity

class MainActivity : Activity() {
    private val requestCapture = 901
    private lateinit var url: EditText
    private lateinit var key: EditText
    private lateinit var start: Button
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val layout = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(32, 40, 32, 24) }
        layout.addView(TextView(this).apply { text = "Government AI Platform – Phone Monitor\n\nEnter the HTTPS monitor service URL and the pairing key provided by your administrator. Screen sharing starts only after your approval."; textSize = 17f })
        url = EditText(this).apply { hint = "https://your-monitor-service.example.com"; setSingleLine(true) }; layout.addView(url)
        key = EditText(this).apply { hint = "Pairing key"; setSingleLine(true) }; layout.addView(key)
        start = Button(this).apply { text = "Start Screen Sharing" }; layout.addView(start)
        layout.addView(Button(this).apply { text = "Stop Sharing"; setOnClickListener { stopService(Intent(this@MainActivity, MonitorService::class.java)); Toast.makeText(this@MainActivity, "Sharing stopped", Toast.LENGTH_SHORT).show() } })
        layout.addView(TextView(this).apply { text = "While sharing, Android displays its screen-capture indicator/notification. You can stop sharing here or from the Android system notification. The app does not collect audio, camera images, messages, or touch input." })
        setContentView(ScrollView(this).apply { addView(layout) })
        start.setOnClickListener {
            val base = url.text.toString().trim().trimEnd('/')
            val secret = key.text.toString().trim()
            if (!base.startsWith("https://") || secret.length < 32) { Toast.makeText(this, "Enter an HTTPS URL and valid pairing key", Toast.LENGTH_LONG).show(); return@setOnClickListener }
            val manager = getSystemService(MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
            startActivityForResult(manager.createScreenCaptureIntent(), requestCapture)
        }
    }
    @Deprecated("Android activity result API compatibility")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == requestCapture && resultCode == Activity.RESULT_OK && data != null) {
            val service = Intent(this, MonitorService::class.java).apply {
                putExtra("resultCode", resultCode); putExtra("data", data)
                putExtra("baseUrl", url.text.toString().trim().trimEnd('/')); putExtra("apiKey", key.text.toString().trim())
            }
            startForegroundService(service)
            Toast.makeText(this, "Screen sharing started", Toast.LENGTH_SHORT).show()
        } else if (requestCode == requestCapture) Toast.makeText(this, "Screen sharing permission was not granted", Toast.LENGTH_LONG).show()
    }
}
