import { Compass } from "lucide-react";
import { LinkButton } from "../components/ui/Button";
import { Card } from "../components/ui/Card";
import { EmptyState } from "../components/ui/Feedback";

export default function NotFoundPage() {
  return (
    <Card>
      <EmptyState
        icon={<Compass className="size-7" />}
        title="این صفحه پیدا نشد"
        description="نشانی را بررسی کنید یا به نمای کلی برگردید."
        action={<LinkButton to="/">رفتن به نمای کلی</LinkButton>}
      />
    </Card>
  );
}
