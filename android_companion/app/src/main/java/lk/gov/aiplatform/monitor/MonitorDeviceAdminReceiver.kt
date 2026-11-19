package lk.gov.aiplatform.monitor

import android.app.admin.DeviceAdminReceiver
import android.content.Context
import android.content.Intent
import android.widget.Toast

/** DPC entry point. Device Owner status is granted only through Android managed provisioning. */
class MonitorDeviceAdminReceiver : DeviceAdminReceiver() {
    override fun onEnabled(context: Context, intent: Intent) {
        super.onEnabled(context, intent)
        Toast.makeText(context, "Managed device administration enabled", Toast.LENGTH_LONG).show()
        RecoveryScheduler.schedule(context)
    }

    override fun onDisabled(context: Context, intent: Intent) {
        super.onDisabled(context, intent)
        Toast.makeText(context, "Device administration disabled", Toast.LENGTH_LONG).show()
    }
}
