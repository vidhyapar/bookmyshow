from django.db import models
from django.utils import timezone
from datetime import timedelta



class Movie(models.Model):
    title = models.CharField(max_length=200, db_index=True)  

    genre = models.CharField(max_length=100, db_index=True)
    language = models.CharField(max_length=100, db_index=True)
    rating = models.FloatField(db_index=True)

    poster_url = models.URLField(blank=True, null=True)
    trailer_url = models.URLField(blank=True, null=True)

    class Meta:
        indexes = [
            models.Index(fields=['genre', 'language']),  
        ]

    def __str__(self):
        return self.title

#  PAYMENT MODEL
class Payment(models.Model):
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE)

    order_id = models.CharField(max_length=100, unique=True, db_index=True)
    payment_id = models.CharField(max_length=100, blank=True, null=True)
    signature = models.TextField(blank=True, null=True)

    amount = models.IntegerField()

    status = models.CharField(max_length=20, default="created", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    def __str__(self):
        return f"{self.movie.title} - {self.order_id}"



#  SEAT MODEL
class Seat(models.Model):
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE)
    seat_number = models.CharField(max_length=10)

    status = models.CharField(max_length=20, default="available", db_index=True)

    class Meta:
        unique_together = ['movie', 'seat_number']  

    def __str__(self):
        return f"{self.movie.title} - {self.seat_number}"

# RESERVATION MODEL 
class Reservation(models.Model):
    seat = models.ForeignKey(Seat, on_delete=models.CASCADE)
    user_session = models.CharField(max_length=100)
    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, null=True)

    reserved_at = models.DateTimeField(auto_now_add=True)

    # AUTO EXPIRY (2 MINUTES)
    expires_at = models.DateTimeField()

    is_active = models.BooleanField(default=True)

    def save(self, *args, **kwargs):
        if not self.expires_at:
            self.expires_at = timezone.now() + timedelta(minutes=2)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.seat.seat_number} - {self.user_session}"