from django.test import TestCase






from datetime import timedelta
from unittest.mock import patch
from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework.test import APIClient
from .models import Author, Book, Loan, Member
from .tasks import check_overdue_loans


class LendingWorkflowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username="demo-librarian", email="reader@example.invalid")
        self.member = Member.objects.create(user=self.user)
        self.author = Author.objects.create(first_name="Sample", last_name="Author")
        self.book = Book.objects.create(title="Example book", author=self.author, isbn="9780000000002", genre="fiction", available_copies=1)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.book_url = f"/api/books/{self.book.pk}"

    def test_anonymous_requests_are_denied(self):
        self.client.force_authenticate(None)
        self.assertIn(self.client.get("/api/books/").status_code, [401, 403])

    @patch("library.views.send_loan_notification.delay")
    def test_loan_updates_stock_and_queues_mail_after_commit(self, notify):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.book_url + "/loan/", {"member_id": self.member.pk}, format="json")
        self.assertEqual(response.status_code, 201)
        self.book.refresh_from_db()
        self.assertEqual(self.book.available_copies, 0)
        loan = Loan.objects.get(pk=response.data["loan_id"])
        self.assertEqual(loan.due_date, timezone.now().date() + timedelta(days=14))
        notify.assert_called_once_with(loan.pk)

    def test_unavailable_book_does_not_create_a_loan(self):
        self.book.available_copies = 0
        self.book.save(update_fields=["available_copies"])
        response = self.client.post(self.book_url + "/loan/", {"member_id": self.member.pk}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Loan.objects.exists())

    def test_duplicate_active_loan_is_rejected(self):
        Loan.objects.create(book=self.book, member=self.member)
        response = self.client.post(self.book_url + "/loan/", {"member_id": self.member.pk}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Loan.objects.count(), 1)
        self.book.refresh_from_db()
        self.assertEqual(self.book.available_copies, 1)

    def test_return_replenishes_stock_once(self):
        loan = Loan.objects.create(book=self.book, member=self.member)
        self.book.available_copies = 0
        self.book.save(update_fields=["available_copies"])
        payload = {"member_id": self.member.pk}
        self.assertEqual(self.client.post(self.book_url + "/return_book/", payload).status_code, 200)
        loan.refresh_from_db()
        self.assertTrue(loan.is_returned)
        self.assertEqual(loan.return_date, timezone.now().date())
        self.assertEqual(self.client.post(self.book_url + "/return_book/", payload).status_code, 400)
        self.book.refresh_from_db()
        self.assertEqual(self.book.available_copies, 1)

    def test_direct_loan_creation_cannot_bypass_stock_handling(self):
        self.assertEqual(self.client.post("/api/loans/", {}, format="json").status_code, 405)

    def test_extension_returns_the_updated_due_date(self):
        loan = Loan.objects.create(book=self.book, member=self.member)
        expected = loan.due_date + timedelta(days=3)
        response = self.client.post(f"/api/loans/{loan.pk}/extended_due_date/", {"additional_days": 3}, format="json")
        self.assertEqual(response.status_code, 200)
        loan.refresh_from_db()
        self.assertEqual(loan.due_date, expected)
        self.assertEqual(response.data["due_date"], expected.isoformat())

    def test_invalid_extension_values_do_not_change_due_date(self):
        loan = Loan.objects.create(book=self.book, member=self.member)
        expected = loan.due_date
        for value in [None, 0, -1, "three", True]:
            with self.subTest(value=value):
                response = self.client.post(f"/api/loans/{loan.pk}/extended_due_date/", {"additional_days": value}, format="json")
                self.assertEqual(response.status_code, 400)
                loan.refresh_from_db()
                self.assertEqual(loan.due_date, expected)

    def test_returned_and_overdue_loans_cannot_be_extended(self):
        loan = Loan.objects.create(book=self.book, member=self.member, is_returned=True)
        url = f"/api/loans/{loan.pk}/extended_due_date/"
        self.assertEqual(self.client.post(url, {"additional_days": 1}, format="json").status_code, 400)
        loan.is_returned = False
        loan.due_date = timezone.now().date() - timedelta(days=1)
        loan.save()
        self.assertEqual(self.client.post(url, {"additional_days": 1}, format="json").status_code, 400)

    @patch("library.tasks.send_mail")
    def test_only_active_past_due_loans_receive_reminders(self, mail):
        today = timezone.now().date()
        Loan.objects.create(book=self.book, member=self.member, due_date=today - timedelta(days=1))
        Loan.objects.create(book=self.book, member=self.member, due_date=today)
        Loan.objects.create(book=self.book, member=self.member, due_date=today - timedelta(days=2), is_returned=True)
        check_overdue_loans.run()
        self.assertEqual(mail.call_count, 1)

    def test_invalid_member_does_not_create_a_loan(self):
        response = self.client.post(self.book_url + "/loan/", {"member_id": "not-an-id"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Loan.objects.exists())
