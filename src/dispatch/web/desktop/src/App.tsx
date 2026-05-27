import { Routes, Route, Navigate } from "react-router-dom";
import { AuthGate } from "./components/AuthGate";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { Shell } from "./components/Shell";
import Inbox from "./pages/Inbox";
import People from "./pages/People";
import Saved from "./pages/Saved";
import History from "./pages/History";
import Devices from "./pages/Devices";
import DispatchDetail from "./pages/DispatchDetail";

export default function App() {
  return (
    <ErrorBoundary>
      <AuthGate>
        <Routes>
          <Route element={<Shell />}>
            <Route index element={<Navigate to="/inbox" replace />} />
            <Route path="/inbox" element={<Inbox />} />
            <Route path="/dispatch/:id" element={<DispatchDetail />} />
            <Route path="/people" element={<People />} />
            <Route path="/saved" element={<Saved />} />
            <Route path="/history" element={<History />} />
            <Route path="/devices" element={<Devices />} />
            <Route path="*" element={<Navigate to="/inbox" replace />} />
          </Route>
        </Routes>
      </AuthGate>
    </ErrorBoundary>
  );
}
