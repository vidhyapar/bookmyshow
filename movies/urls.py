from django.urls import path
from . import views

urlpatterns = [
    path('', views.movie_list, name='movie_list'),

    # 🎟 Booking
    path('book/', views.book_ticket, name='book_ticket'),

    #  Payment
    path('pay/', views.create_order, name='pay'),
    path('verify/', views.verify_payment, name='verify'),

    #  Webhook
    path('webhook/', views.razorpay_webhook, name='webhook'),
    path('test-email/', views.test_email),
    
    path('select-seats/', views.select_seats, name='select_seats'),
    path('confirm-booking/', views.confirm_booking, name='confirm_booking'),
    path('create-order/', views.create_order, name='create_order'),
]