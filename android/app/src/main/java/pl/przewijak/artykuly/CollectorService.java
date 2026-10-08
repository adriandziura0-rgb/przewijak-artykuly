package pl.przewijak.artykuly;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.os.Build;
import android.os.IBinder;

import com.chaquo.python.Python;

import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

public class CollectorService extends Service {
    private static final String CHANNEL = "przewijak_praca";
    private ScheduledExecutorService scheduler;

    @Override
    public void onCreate() {
        super.onCreate();
        createChannel();
        Notification.Builder builder = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CHANNEL)
                : new Notification.Builder(this);
        builder.setContentTitle("Przewijak — ARTYKUŁY")
                .setContentText("Silnik działa w tle")
                .setSmallIcon(android.R.drawable.stat_notify_sync)
                .setOngoing(true);
        startForeground(153, builder.build());

        try {
            Python.getInstance().getModule("mobile_entry").callAttr("start_server");
        } catch (Exception ignored) {}

        scheduler = Executors.newSingleThreadScheduledExecutor();
        scheduler.scheduleWithFixedDelay(() -> {
            try { SafSync.syncAll(this); } catch (Exception ignored) {}
        }, 10, 30, TimeUnit.SECONDS);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        return START_STICKY;
    }

    private void createChannel() {
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel ch = new NotificationChannel(
                    CHANNEL, "Przewijak — praca w tle", NotificationManager.IMPORTANCE_LOW);
            ch.setDescription("Utrzymuje kolektor artykułów podczas pracy w tle.");
            getSystemService(NotificationManager.class).createNotificationChannel(ch);
        }
    }

    @Override
    public void onDestroy() {
        if (scheduler != null) scheduler.shutdownNow();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
