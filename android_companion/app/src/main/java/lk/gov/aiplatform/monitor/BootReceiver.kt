package lk.gov.aiplatform.monitor

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.content.ContextCompat

/**
 * Runs after the phone restarts (or the app is updated). It does TWO things only:
 *  1. Schedules the network-only recovery heartbeat (RecoveryWorker) - no screen content, ever.
 *  2. Shows a normal, visible notification inviting the owner to open the app and resume sharing.
 *
 * It deliberately does NOT start screen capture by itself: Android always asks the user to
 * approve screen sharing again after every restart, and this app keeps that rule. Tapping the
 * notification only opens the app's own screen - the owner still presses "Start Screen Sharing".
 */
class BootReceiver : BroadcastReceiver() {

    private val reminderChannelId = "monitor_reminder"

    override fun onReceive(context: Context, intent: Intent?) {
        when (intent?.action) {
            Intent.ACTION_BOOT_COMPLETED,
            Intent.ACTION_MY_PACKAGE_REPLACED -> {
                RecoveryScheduler.schedule(context)
                showResumeReminder(context)
            }
        }
    }

    private fun showResumeReminder(context: Context) {
        // Android 13+ requires this permission before any notification can be shown; if it was
        // never granted (or was revoked), there is nothing to post and nothing silent happens either.
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED
        ) {
            return
        }

        val manager = context.getSystemService(NotificationManager::class.java) ?: return
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            manager.createNotificationChannel(
                NotificationChannel(
                    reminderChannelId,
                    "Resume screen sharing reminder",
                    NotificationManager.IMPORTANCE_DEFAULT,
                ).apply { description = "Shown once after the phone restarts." }
            )
        }

        val openApp = PendingIntent.getActivity(
            context,
            2,
            Intent(context, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
            },
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )

        val notification = Notification.Builder(context, reminderChannelId)
            .setContentTitle("Government AI Mobile Monitor")
            .setContentText("The phone restarted, so screen sharing stopped. Tap to open and start it again.")
            .setSmallIcon(android.R.drawable.ic_menu_view)
            .setContentIntent(openApp)
            .setAutoCancel(true)
            .build()

        manager.notify(1001, notification)
    }
}
