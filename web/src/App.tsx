import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { BudgetProvider } from "./context/BudgetContext";
import { Layout } from "./components/Layout";
import { OverviewPage } from "./pages/OverviewPage";
import { RecordPage } from "./pages/RecordPage";
import { RecurringPage } from "./pages/RecurringPage";
import { SetupPage } from "./pages/SetupPage";

export default function App() {
  return (
    <BudgetProvider>
      <BrowserRouter>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<Navigate to="/setup" replace />} />
            <Route path="setup" element={<SetupPage />} />
            <Route path="recurring" element={<RecurringPage />} />
            <Route path="record" element={<RecordPage />} />
            <Route path="overview" element={<OverviewPage />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </BudgetProvider>
  );
}
