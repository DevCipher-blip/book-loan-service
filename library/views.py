from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import Author, Book, Member, Loan
from .serializers import AuthorSerializer, BookSerializer, MemberSerializer, LoanSerializer
from .tasks import send_loan_notification


class AuthorViewSet(viewsets.ModelViewSet):
    queryset = Author.objects.all()
    serializer_class = AuthorSerializer


class BookViewSet(viewsets.ModelViewSet):
    queryset = Book.objects.select_related("author").all()
    serializer_class = BookSerializer

    @action(detail=True, methods=["post"])
    def loan(self, request, pk=None):
        selected = self.get_object()
        try:
            member = Member.objects.get(pk=request.data.get("member_id"))
        except (Member.DoesNotExist, ValueError, TypeError):
            return Response({"error": "A valid member_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            book = Book.objects.select_for_update().get(pk=selected.pk)
            if book.available_copies < 1:
                return Response({"error": "No available copies."}, status=status.HTTP_400_BAD_REQUEST)
            if Loan.objects.filter(book=book, member=member, is_returned=False).exists():
                return Response({"error": "This member already has an active loan for this book."}, status=status.HTTP_400_BAD_REQUEST)
            loan = Loan.objects.create(book=book, member=member)
            book.available_copies -= 1
            book.save(update_fields=["available_copies"])
            transaction.on_commit(lambda: send_loan_notification.delay(loan.pk))
        return Response({"status": "Book loaned successfully.", "loan_id": loan.pk}, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def return_book(self, request, pk=None):
        selected = self.get_object()
        with transaction.atomic():
            book = Book.objects.select_for_update().get(pk=selected.pk)
            try:
                loan = Loan.objects.filter(book=book, member_id=request.data.get("member_id"), is_returned=False).order_by("loan_date", "pk").first()
            except (ValueError, TypeError):
                loan = None
            if loan is None:
                return Response({"error": "Active loan does not exist."}, status=status.HTTP_400_BAD_REQUEST)
            loan.is_returned = True
            loan.return_date = timezone.now().date()
            loan.save(update_fields=["is_returned", "return_date"])
            book.available_copies += 1
            book.save(update_fields=["available_copies"])
        return Response({"status": "Book returned successfully."}, status=status.HTTP_200_OK)


class MemberViewSet(viewsets.ModelViewSet):
    queryset = Member.objects.select_related("user").all()
    serializer_class = MemberSerializer


class LoanViewSet(viewsets.ReadOnlyModelViewSet):
    # Lending actions own stock changes; direct loan CRUD would bypass that invariant.
    queryset = Loan.objects.select_related("book__author", "member__user").all()
    serializer_class = LoanSerializer

    @action(detail=True, methods=["post"], url_path="extended_due_date")
    def extended_due_date(self, request, pk=None):
        with transaction.atomic():
            loan = Loan.objects.select_for_update().get(pk=self.get_object().pk)
            if loan.is_returned or loan.due_date < timezone.now().date():
                return Response({"error": "Returned or overdue loans cannot be extended."}, status=status.HTTP_400_BAD_REQUEST)
            days = request.data.get("additional_days")
            if isinstance(days, bool) or not isinstance(days, int) or days < 1:
                return Response({"error": "additional_days must be a positive integer."}, status=status.HTTP_400_BAD_REQUEST)
            try:
                loan.due_date += timedelta(days=days)
            except (OverflowError, ValueError):
                return Response({"error": "The requested due date is out of range."}, status=status.HTTP_400_BAD_REQUEST)
            loan.save(update_fields=["due_date"])
        return Response(self.get_serializer(loan).data, status=status.HTTP_200_OK)
