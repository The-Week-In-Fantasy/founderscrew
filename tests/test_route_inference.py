from founderscrew.tools.route_inference import infer_qa_route_candidates, format_route_candidates


def test_infer_qa_routes_from_component_import_consumers(tmp_path):
    (tmp_path / "src/components").mkdir(parents=True)
    (tmp_path / "src/pages").mkdir(parents=True)
    (tmp_path / "src/App.jsx").write_text(
        """
import DashboardPage from './pages/DashboardPage';
import DraftAssistantPage from './pages/DraftAssistantPage';
import DraftPlannerPage from './pages/DraftPlannerPage';

export default function App() {
  return (
    <Routes>
      <Route path="/dashboard" element={<DashboardPage />} />
      <Route
        path="/draft"
        element={<FeatureFlagGate flag="draft_assistant"><DraftAssistantPage /></FeatureFlagGate>}
      />
      <Route
        path="/draftplan"
        element={<FeatureFlagGate flag="draft_planner"><DraftPlannerPage /></FeatureFlagGate>}
      />
    </Routes>
  );
}
""",
        encoding="utf-8",
    )
    (tmp_path / "src/components/DraftPlayerBoard.jsx").write_text(
        "export default function DraftPlayerBoard() { return <div />; }\n",
        encoding="utf-8",
    )
    (tmp_path / "src/pages/DraftAssistantPage.jsx").write_text(
        "import DraftPlayerBoard from '../components/DraftPlayerBoard';\nexport default function DraftAssistantPage() { return <DraftPlayerBoard />; }\n",
        encoding="utf-8",
    )
    (tmp_path / "src/pages/DraftPlannerPage.jsx").write_text(
        "import DraftPlayerBoard from '../components/DraftPlayerBoard';\nexport default function DraftPlannerPage() { return <DraftPlayerBoard />; }\n",
        encoding="utf-8",
    )
    (tmp_path / "src/pages/DashboardPage.jsx").write_text(
        "export default function DashboardPage() { return <main />; }\n",
        encoding="utf-8",
    )

    candidates = infer_qa_route_candidates(
        str(tmp_path),
        issue_title="Summaries cut off on DraftPlayerBoard in draft assistant",
        affected_files=["src/components/DraftPlayerBoard.jsx"],
    )

    paths = [candidate["path"] for candidate in candidates]
    assert "/draft" in paths
    assert "/draftplan" in paths
    assert "/dashboard" not in paths
    assert "DraftPlayerBoard" in format_route_candidates(candidates)


def test_infer_qa_routes_does_not_spray_from_app_route_table(tmp_path):
    (tmp_path / "src/components").mkdir(parents=True)
    (tmp_path / "src/pages").mkdir(parents=True)
    (tmp_path / "src/App.jsx").write_text(
        """
import AboutUsPage from './pages/AboutUsPage';
import AddTeamPage from './pages/AddTeamPage';
import DraftAssistantPage from './pages/DraftAssistantPage';
import DraftPlannerPage from './pages/DraftPlannerPage';

<Route path="/about" element={<AboutUsPage />} />
<Route path="/addteam" element={<AddTeamPage />} />
<Route path="/draft" element={<DraftAssistantPage />} />
<Route path="/draftplan" element={<DraftPlannerPage />} />
""",
        encoding="utf-8",
    )
    (tmp_path / "src/components/DraftPlayerBoard.jsx").write_text(
        "export default function DraftPlayerBoard() { return <div />; }\n",
        encoding="utf-8",
    )
    (tmp_path / "src/pages/DraftAssistantPage.jsx").write_text(
        "import DraftPlayerBoard from '../components/DraftPlayerBoard';\nexport default function DraftAssistantPage() { return <DraftPlayerBoard />; }\n",
        encoding="utf-8",
    )
    (tmp_path / "src/pages/DraftPlannerPage.jsx").write_text(
        "import DraftPlayerBoard from '../components/DraftPlayerBoard';\nexport default function DraftPlannerPage() { return <DraftPlayerBoard />; }\n",
        encoding="utf-8",
    )
    (tmp_path / "src/pages/AboutUsPage.jsx").write_text("export default function AboutUsPage() { return <main />; }\n", encoding="utf-8")
    (tmp_path / "src/pages/AddTeamPage.jsx").write_text("export default function AddTeamPage() { return <main />; }\n", encoding="utf-8")

    candidates = infer_qa_route_candidates(
        str(tmp_path),
        issue_title="DraftPlayerBoard summaries cut off in draft assistant",
        affected_files=["src/components/DraftPlayerBoard.jsx"],
        changed_files=["src/App.jsx"],
    )

    paths = [candidate["path"] for candidate in candidates]
    assert paths[0] == "/draft"
    assert "/draftplan" in paths
    assert "/about" not in paths
    assert "/addteam" not in paths


def test_infer_qa_routes_follows_nested_component_import_chain(tmp_path):
    (tmp_path / "src/components").mkdir(parents=True)
    (tmp_path / "src/pages").mkdir(parents=True)
    (tmp_path / "src/App.jsx").write_text(
        """
import DraftPlannerPage from './pages/DraftPlannerPage';

<Route path="/draftplan" element={<DraftPlannerPage />} />
""",
        encoding="utf-8",
    )
    (tmp_path / "src/components/DraftPlayerBoard.jsx").write_text(
        "export default function DraftPlayerBoard() { return <div />; }\n",
        encoding="utf-8",
    )
    (tmp_path / "src/components/SnakeDraftPlanner.jsx").write_text(
        "import DraftPlayerBoard from './DraftPlayerBoard';\nexport default function SnakeDraftPlanner() { return <DraftPlayerBoard />; }\n",
        encoding="utf-8",
    )
    (tmp_path / "src/pages/DraftPlannerPage.jsx").write_text(
        "import SnakeDraftPlanner from '../components/SnakeDraftPlanner';\nexport default function DraftPlannerPage() { return <SnakeDraftPlanner />; }\n",
        encoding="utf-8",
    )

    candidates = infer_qa_route_candidates(
        str(tmp_path),
        issue_title="DraftPlayerBoard summaries cut off in draft planner",
        affected_files=["src/components/DraftPlayerBoard.jsx"],
        changed_files=["src/App.jsx"],
    )

    assert [candidate["path"] for candidate in candidates] == ["/draftplan"]
    assert "DraftPlayerBoard.jsx -> src/components/SnakeDraftPlanner.jsx -> src/pages/DraftPlannerPage.jsx" in candidates[0]["reason"]


def test_infer_qa_routes_uses_issue_component_name_without_app_spraying(tmp_path):
    (tmp_path / "src/components").mkdir(parents=True)
    (tmp_path / "src/pages").mkdir(parents=True)
    (tmp_path / "tests/integration").mkdir(parents=True)
    (tmp_path / "src/App.jsx").write_text(
        """
import AddTeamPage from './pages/AddTeamPage';
import DraftAssistantPage from './pages/DraftAssistantPage';

<Route path="/addteam" element={<AddTeamPage />} />
<Route path="/draft" element={<DraftAssistantPage />} />
""",
        encoding="utf-8",
    )
    (tmp_path / "src/components/DraftPlayerBoard.jsx").write_text(
        "export default function DraftPlayerBoard() { return <div />; }\n",
        encoding="utf-8",
    )
    (tmp_path / "src/pages/DraftAssistantPage.jsx").write_text(
        "import DraftPlayerBoard from '../components/DraftPlayerBoard';\nexport default function DraftAssistantPage() { return <DraftPlayerBoard />; }\n",
        encoding="utf-8",
    )
    (tmp_path / "src/pages/AddTeamPage.jsx").write_text(
        "export default function AddTeamPage() { return <main />; }\n",
        encoding="utf-8",
    )
    (tmp_path / "tests/integration/issue_327_test.spec.js").write_text(
        "test('summary scroll', async () => {});\n",
        encoding="utf-8",
    )

    candidates = infer_qa_route_candidates(
        str(tmp_path),
        issue_title="DraftPlayerBoard summaries cut off in draft assistant",
        affected_files=["tests/integration/issue_327_test.spec.js"],
        changed_files=["src/App.jsx"],
    )

    assert [candidate["path"] for candidate in candidates] == ["/draft"]
