package site.xfoot.app;

import android.os.Bundle;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        // Enregistrement AVANT super.onCreate() — requis par Capacitor pour
        // qu'un plugin local au projet (non publié sur npm) soit disponible
        // au chargement de la WebView (voir GooglePlayBillingPlugin.java).
        registerPlugin(GooglePlayBillingPlugin.class);
        super.onCreate(savedInstanceState);
    }
}
