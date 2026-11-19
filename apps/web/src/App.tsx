import { createBrowserRouter, Navigate, RouterProvider } from "react-router-dom";
import { Shell } from "./components/Shell";
import { Dashboard } from "./pages/Dashboard";
import { Insights } from "./pages/Insights";
import { Live } from "./pages/Live";
import { LiveMonitor } from "./pages/LiveMonitor";
import { NewClip } from "./pages/NewClip";
import { ProjectDetail } from "./pages/ProjectDetail";

const router = createBrowserRouter([
  {
    path: "/",
    element: <Shell />,
    children: [
      { index: true, element: <Dashboard /> },
      { path: "new", element: <NewClip /> },
      { path: "projects/:id", element: <ProjectDetail /> },
      { path: "live", element: <Live /> },
      { path: "live/:id", element: <LiveMonitor /> },
      { path: "insights", element: <Insights /> },
      { path: "*", element: <Navigate to="/" replace /> },
    ],
  },
]);

export function App() {
  return <RouterProvider router={router} />;
}