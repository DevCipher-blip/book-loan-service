from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from .models import Loan


@shared_task
def send_loan_notification(loan_id):
    try:
        loan = Loan.objects.select_related("book", "member__user").get(pk=loan_id)
    except Loan.DoesNotExist:
        return
    email = loan.member.user.email
    if not email:
        return
    send_mail(
        subject="Book loaned successfully",
        message=f'You have borrowed "{loan.book.title}". Please return it by {loan.due_date}.',
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[email],
        fail_silently=False,
    )


@shared_task
def check_overdue_loans():
    loans = Loan.objects.filter(is_returned=False, due_date__lt=timezone.now().date()).select_related("book", "member__user")
    for loan in loans:
        email = loan.member.user.email
        if email:
            send_mail(
                subject="Overdue book loan",
                message=f'Please return "{loan.book.title}"; it was due on {loan.due_date}.',
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[email],
                fail_silently=False,
            )
