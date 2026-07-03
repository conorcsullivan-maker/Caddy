import { useEffect, useState } from "react";
import { ActivityIndicator, View } from "react-native";
import { StatusBar } from "expo-status-bar";
import { api, User } from "./src/api";
import { clearToken, getToken } from "./src/auth";
import { colors } from "./src/theme";
import LoginScreen from "./src/screens/LoginScreen";
import ChatScreen from "./src/screens/ChatScreen";

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [booting, setBooting] = useState(true);

  useEffect(() => {
    (async () => {
      try {
        if (await getToken()) {
          const { user } = await api.me();
          setUser(user);
        }
      } catch {
        await clearToken(); // expired or revoked session
      } finally {
        setBooting(false);
      }
    })();
  }, []);

  if (booting) {
    return (
      <View style={{ flex: 1, backgroundColor: colors.cream, justifyContent: "center" }}>
        <ActivityIndicator color={colors.forest} size="large" />
      </View>
    );
  }

  return (
    <>
      <StatusBar style={user ? "light" : "dark"} />
      {user ? (
        <ChatScreen user={user} onLogout={() => setUser(null)} />
      ) : (
        <LoginScreen onLogin={setUser} />
      )}
    </>
  );
}
