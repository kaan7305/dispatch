import { Routes, Route, Navigate } from "react-router-dom";
import { AuthGate } from "./components/AuthGate";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { Shell } from "./components/Shell";
import Inbox from "./pages/Inbox";
import People from "./pages/People";
import History from "./pages/History";
import Devices from "./pages/Devices";
import DispatchDetail from "./pages/DispatchDetail";
import Workflows from "./pages/Workflows";
import WorkflowEditor from "./pages/WorkflowEditor";
import WorkflowRun from "./pages/WorkflowRun";
import Contexts from "./pages/Contexts";
import ContextEditor from "./pages/ContextEditor";
import Settings from "./pages/Settings";

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
            <Route path="/workflows" element={<Workflows />} />
            <Route path="/workflows/new" element={<WorkflowEditor />} />
            <Route path="/workflows/:id/edit" element={<WorkflowEditor />} />
            <Route path="/runs/:runId" element={<WorkflowRun />} />
            <Route path="/contexts" element={<Contexts />} />
            <Route path="/contexts/new" element={<ContextEditor />} />
            <Route path="/contexts/:id/edit" element={<ContextEditor />} />
            <Route path="/history" element={<History />} />
            <Route path="/devices" element={<Devices />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="*" element={<Navigate to="/inbox" replace />} />
          </Route>
        </Routes>
      </AuthGate>
    </ErrorBoundary>
  );
}
