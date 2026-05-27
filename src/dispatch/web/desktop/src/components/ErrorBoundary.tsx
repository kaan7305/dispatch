import { Component, type ReactNode } from "react";
import { Button } from "./ui/button";

interface State { error: Error | null; }

export class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: { componentStack: string }) {
    // eslint-disable-next-line no-console
    console.error("Dispatch UI error:", error, info.componentStack);
  }

  reset = () => this.setState({ error: null });

  render() {
    if (!this.state.error) return this.props.children;

    return (
      <div className="min-h-full grid place-items-center p-8">
        <div className="max-w-2xl w-full space-y-4 rounded-lg border bg-card p-6">
          <h1 className="text-xl font-semibold">Something broke in the UI</h1>
          <p className="text-sm text-muted-foreground">
            The daemon is still running. This is a render error in the desktop
            app — usually a bug in our code, not yours.
          </p>
          <pre className="text-xs bg-muted p-3 rounded-md overflow-x-auto max-h-64 whitespace-pre-wrap">
            {this.state.error.message}
            {"\n\n"}
            {this.state.error.stack ?? ""}
          </pre>
          <div className="flex gap-2">
            <Button onClick={this.reset}>Try again</Button>
            <Button variant="outline" onClick={() => location.reload()}>
              Reload
            </Button>
          </div>
        </div>
      </div>
    );
  }
}
