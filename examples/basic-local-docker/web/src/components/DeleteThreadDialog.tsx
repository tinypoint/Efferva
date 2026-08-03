import {
  AlertDialog,
  AlertDialogClose,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";

type DeleteThreadDialogProps = {
  title: string | null;
  pending: boolean;
  error: unknown;
  onClose: () => void;
  onConfirm: () => void;
};

export function DeleteThreadDialog({
  title,
  pending,
  error,
  onClose,
  onConfirm,
}: DeleteThreadDialogProps) {
  return (
    <AlertDialog
      open={title !== null}
      onOpenChange={(open) => {
        if (!open && !pending) onClose();
      }}
    >
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Delete thread?</AlertDialogTitle>
          <AlertDialogDescription>
            “{title ?? ""}” will be permanently deleted. This action cannot be
            undone.
          </AlertDialogDescription>
        </AlertDialogHeader>
        {error !== null && error !== undefined && (
          <p className="text-sm text-destructive" role="alert">
            {error instanceof Error
              ? error.message
              : "Failed to delete the thread"}
          </p>
        )}
        <AlertDialogFooter>
          <AlertDialogClose
            render={<Button variant="outline" disabled={pending} />}
          >
            Cancel
          </AlertDialogClose>
          <Button
            type="button"
            variant="destructive"
            disabled={title === null || pending}
            onClick={onConfirm}
          >
            {pending ? "Deleting…" : "Delete"}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
