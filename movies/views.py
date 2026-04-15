from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import render, redirect
from .models import Movie, Payment, Seat, Reservation
from django.core.paginator import Paginator
from django.http import HttpResponse, JsonResponse
import logging
import razorpay
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
from django.shortcuts import get_object_or_404

from django.utils import timezone
from datetime import timedelta
from django.db.models import Count, Sum
from django.db.models.functions import TruncHour
from django.db import transaction
from django.core.mail import send_mail
from django.core.cache import cache


logger = logging.getLogger(__name__)
client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))


#  ADMIN CHECK
def is_admin(user):
    return user.is_staff
def movie_list(request):
    search = request.GET.get('search')
    genres = request.GET.getlist('genre')
    languages = request.GET.getlist('language')
    sort = request.GET.get('sort')

    # Base queryset (ONLY ONE — FIXED)
    movies = Movie.objects.all().only(
        'title', 'genre', 'language', 'rating', 'poster_url', 'trailer_url'
    )

    # SEARCH FILTER
    if search:
        movies = movies.filter(title__icontains=search)

    # MULTI FILTERS
    if genres:
        movies = movies.filter(genre__in=genres)

    if languages:
        movies = movies.filter(language__in=languages)

    # SORTING
    if sort == 'rating':
        movies = movies.order_by('-rating')
    else:
        movies = movies.order_by('id')

    # PAGINATION
    paginator = Paginator(movies, 8)
    page_obj = paginator.get_page(request.GET.get('page'))

   

    base = Movie.objects.all()

    if search:
        base = base.filter(title__icontains=search)

    # genre counts depend on current language filter
    genre_base = base
    if languages:
        genre_base = genre_base.filter(language__in=languages)

    genre_counts = genre_base.values('genre').annotate(count=Count('id')).order_by()

    # language counts depend on current genre filter
    lang_base = base
    if genres:
        lang_base = lang_base.filter(genre__in=genres)

    language_counts = lang_base.values('language').annotate(count=Count('id')).order_by()

    return render(request, 'movies.html', {
        'movies': page_obj,
        'page_obj': page_obj,
        'selected_genres': genres,
        'selected_languages': languages,
        'sort': sort,
        'search': search,
        'genre_counts': genre_counts,
        'language_counts': language_counts
    })




# SELECT + LOCK SEATS
def select_seats(request):
    release_expired_seats()

    movie_id = request.GET.get("movie_id")
    seat_numbers = request.GET.getlist("seats")

    if not movie_id:
        return JsonResponse({"error": "Movie ID missing"}, status=400)

    if not seat_numbers:
        movie = Movie.objects.get(id=movie_id)
        return render(request, "select_seats.html", {"movie": movie})

    if not request.session.session_key:
        request.session.create()

    user_session = request.session.session_key

    locked_seats = []
    failed_seats = []

    with transaction.atomic():
        for seat_no in seat_numbers:

            seat, created = Seat.objects.select_for_update().get_or_create(
                movie_id=movie_id,
                seat_number=seat_no,
                defaults={"status": "available"}
            )

            active_reservation = Reservation.objects.filter(
                seat=seat,
                is_active=True,
                expires_at__gt=timezone.now()
            ).first()

            if seat.status == "booked" or active_reservation:
                failed_seats.append(seat_no)
                continue

            seat.status = "locked"
            seat.save()

            Reservation.objects.create(
                seat=seat,
                user_session=user_session
            )

            locked_seats.append(seat_no)

    return JsonResponse({
        "locked": locked_seats,
        "failed": failed_seats
    })


# CREATE ORDER
def create_order(request):
    movie_id = request.GET.get('movie_id')
    seat_numbers = request.GET.getlist('seats')

    if not movie_id:
        return HttpResponse("Movie ID missing")

    movie = get_object_or_404(Movie, id=movie_id)
    
    if not request.session.session_key:
        request.session.create()

    user_session = request.session.session_key

    if not seat_numbers:
        return HttpResponse("No seats selected")

    reservations = Reservation.objects.filter(
        user_session=user_session,
        is_active=True,
        expires_at__gt=timezone.now(),
        seat__seat_number__in=seat_numbers
    )

    seat_count = reservations.count()

    if seat_count == 0:
        return HttpResponse("No seats selected")

    seat_numbers = [res.seat.seat_number for res in reservations]

    amount = seat_count * 20000

    order = client.order.create({
        "amount": amount,
        "currency": "INR",
        "payment_capture": "1"
    })

    Payment.objects.create(
        movie=movie,
        order_id=order['id'],
        amount=amount
    )

    return render(request, "payment.html", {
        "order_id": order['id'],
        "display_amount": amount // 100,
        "key": settings.RAZORPAY_KEY_ID,
        "movie": movie.title,
        "seats": seat_numbers,
        "count": seat_count
    })


# VERIFY PAYMENT
@csrf_exempt
def verify_payment(request):
    payment_id = request.GET.get('payment_id')
    order_id = request.GET.get('order_id')
    signature = request.GET.get('signature')

    if not payment_id or not order_id or not signature:
        return HttpResponse("Invalid Request")

    try:
        payment = Payment.objects.get(order_id=order_id)
    except:
        return HttpResponse("Invalid Order")

    if payment.created_at < timezone.now() - timedelta(minutes=10):
        payment.status = "failed"
        payment.save()
        return HttpResponse("Payment Timeout")

    params = {
        'razorpay_order_id': order_id,
        'razorpay_payment_id': payment_id,
        'razorpay_signature': signature
    }

    try:
        client.utility.verify_payment_signature(params)

        payment.payment_id = payment_id
        payment.signature = signature
        payment.status = "paid"
        payment.save()

        return redirect(f"/confirm-booking/?order_id={order_id}")

    except Exception as e:
        payment.status = "failed"
        payment.save()
        logger.error(e)
        return HttpResponse("Payment Failed")


#  CONFIRM BOOKING
def confirm_booking(request):
    order_id = request.GET.get("order_id")

    try:
        payment = Payment.objects.get(order_id=order_id)
    except:
        return HttpResponse("Invalid Order")

    user_session = request.session.session_key
    booked_seats = []

    with transaction.atomic():
        reservations = Reservation.objects.select_for_update().filter(
            user_session=user_session,
            is_active=True,
            expires_at__gt=timezone.now()
        )

        for res in reservations:
            seat = res.seat
            seat.status = "booked"
            seat.save()

            booked_seats.append(seat.seat_number)

            res.is_active = False
            res.save()

    send_mail(
        "🎟 Movie Ticket Confirmed",
        f"Movie: {payment.movie.title}\nSeats: {', '.join(booked_seats)}",
        settings.EMAIL_HOST_USER,
        [settings.EMAIL_HOST_USER],
        fail_silently=False,
    )

    return HttpResponse("🎉 Seats Booked Successfully! Email Sent.")



@csrf_exempt
def razorpay_webhook(request):
    import json
    import hmac
    import hashlib

    if request.method != "POST":
        return HttpResponse("Invalid method", status=405)

    payload = request.body
    received_signature = request.META.get("HTTP_X_RAZORPAY_SIGNATURE")

    webhook_secret = settings.RAZORPAY_WEBHOOK_SECRET

    # Step 1: Verify signature (MOST IMPORTANT SECURITY STEP)
    generated_signature = hmac.new(
        webhook_secret.encode(),
        payload,
        hashlib.sha256
    ).hexdigest()

    if generated_signature != received_signature:
        return HttpResponse("Invalid signature", status=403)

    data = json.loads(payload.decode("utf-8"))
    event = data.get("event")

    if event == "payment.captured":
        payment_entity = data["payload"]["payment"]["entity"]
        order_id = payment_entity["order_id"]
        payment_id = payment_entity["id"]

        try:
            payment = Payment.objects.get(order_id=order_id)
        except Payment.DoesNotExist:
            return HttpResponse("Order not found", status=404)

        # ✅ IDEMPOTENCY CHECK (IMPORTANT)
        if payment.status == "paid":
            return HttpResponse("Already processed", status=200)

        payment.payment_id = payment_id
        payment.status = "paid"
        payment.save()

    # Step 4: Handle failed payment
    elif event == "payment.failed":
        payment_entity = data["payload"]["payment"]["entity"]
        order_id = payment_entity["order_id"]

        Payment.objects.filter(order_id=order_id).update(status="failed")

    return HttpResponse("Webhook processed", status=200)


def book_ticket(request):
    return HttpResponse("Book Ticket Page")


def test_email(request):
    send_mail(
        "Test Email",
        "This is a test email",
        settings.EMAIL_HOST_USER,
        [settings.EMAIL_HOST_USER],
        fail_silently=False,
    )
    return HttpResponse("Email Sent!")


#  AUTO RELEASE EXPIRED SEATS
def release_expired_seats():
    expired_reservations = Reservation.objects.filter(
        is_active=True,
        expires_at__lt=timezone.now()
    )

    for res in expired_reservations:
        seat = res.seat
        seat.status = "available"
        seat.save()

        res.is_active = False
        res.save()


#  ADMIN DASHBOARD 
@login_required
@user_passes_test(is_admin)
def admin_dashboard(request):

    data = cache.get("dashboard_data")

    if not data:

        now = timezone.now()

        total_revenue = Payment.objects.filter(status="paid").aggregate(
            total=Sum('amount')
        )['total'] or 0

        daily_revenue = Payment.objects.filter(
            status="paid",
            created_at__date=now.date()
        ).aggregate(total=Sum('amount'))['total'] or 0

        weekly_revenue = Payment.objects.filter(
            status="paid",
            created_at__gte=now - timedelta(days=7)
        ).aggregate(total=Sum('amount'))['total'] or 0

        monthly_revenue = Payment.objects.filter(
            status="paid",
            created_at__gte=now - timedelta(days=30)
        ).aggregate(total=Sum('amount'))['total'] or 0

        popular_movies = Payment.objects.filter(status="paid") \
            .values('movie__title') \
            .annotate(count=Count('id')) \
            .order_by('-count')[:5]

        peak_hours = Payment.objects.filter(status="paid") \
            .annotate(hour=TruncHour('created_at')) \
            .values('hour') \
            .annotate(count=Count('id')) \
            .order_by('-count')[:5]

        total_payments = Payment.objects.count()
        failed_payments = Payment.objects.filter(status="failed").count()

        cancellation_rate = 0
        if total_payments > 0:
            cancellation_rate = (failed_payments / total_payments) * 100

        data = {
            "total_revenue": total_revenue // 100,
            "daily_revenue": daily_revenue // 100,
            "weekly_revenue": weekly_revenue // 100,
            "monthly_revenue": monthly_revenue // 100,
            "popular_movies": list(popular_movies),
            "peak_hours": list(peak_hours),
            "cancellation_rate": round(cancellation_rate, 2)
        }

        cache.set("dashboard_data", data, 30)

    return render(request, "admin_dashboard.html", data)