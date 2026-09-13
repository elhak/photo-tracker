from django.db import migrations, models
from django.db.models import Count, Max


def merge_shared_orders(apps, schema_editor):
    Tracker = apps.get_model("tracker", "Tracker")
    Photo = apps.get_model("tracker", "Photo")
    db = schema_editor.connection.alias
    duplicates = list(
        Tracker.objects.using(db).values("order_id", "order_month")
        .annotate(total=Count("pk")).filter(total__gt=1)
    )
    for group in duplicates:
        trackers = Tracker.objects.using(db).filter(
            order_id=group["order_id"], order_month=group["order_month"]
        )
        survivor = trackers.order_by("created_at", "pk").first()
        latest = trackers.aggregate(latest=Max("updated_at"))["latest"]
        others = trackers.exclude(pk=survivor.pk)
        Photo.objects.using(db).filter(tracker_id__in=others.values("pk")).update(tracker_id=survivor.pk)
        Tracker.objects.using(db).filter(pk=survivor.pk).update(updated_at=latest)
        others.delete()


class Migration(migrations.Migration):
    dependencies = [("tracker", "0002_remove_tracker_unique_owner_order_and_more")]

    operations = [
        migrations.RemoveConstraint(model_name="tracker", name="unique_owner_order_month"),
        # Attribution survives, but the original tracker grouping cannot be restored.
        migrations.RunPython(merge_shared_orders),
        migrations.AddConstraint(
            model_name="tracker",
            constraint=models.UniqueConstraint(fields=("order_id", "order_month"), name="unique_order_month"),
        ),
    ]
